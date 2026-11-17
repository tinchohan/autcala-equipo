# -*- coding: utf-8 -*-
"""El oído del equipo: el audio de la cámara al oído del servidor de Autcala.

Saca el audio del RTSP de la cámara, detecta la voz y manda SÓLO los tramos
con voz al mismo oído del servidor que hoy usa la caja de la PC
(`/caja/turno/{id}/oido`). El servidor los junta por ventanas, los transcribe
con Whisper y arma los resúmenes de cada ticket y el monto sin ticket. De acá
no sale video ni texto: sólo el audio con voz, y por un instante.

Cada tramo va como WAV mono, 16 bits, 16 kHz, que es lo que el servidor pide
(si no coincide, lo descarta). El equipo abre el «día» de la caja con el PIN,
igual que la PC, y lo mantiene vivo.

La cuenta (la clase Segmentador) y el trato con el servidor (la clase Caja) no
dependen de ffmpeg, de la red ni de una biblioteca de voz: se prueban con
audio y respuestas de mentira (test_oido.py). ffmpeg y webrtcvad se usan sólo
en main().
"""
from __future__ import annotations

import io
import json
import logging
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime, timezone

log = logging.getLogger("oido")

# El formato que pide el servidor. Un tramo que no coincida lo descarta.
RATE = 16000
CANALES = 1
ANCHO = 2                                   # 16 bits
FRAME_MS = 20                               # webrtcvad acepta 10, 20 o 30 ms
FRAME_BYTES = (RATE * FRAME_MS // 1000) * ANCHO      # 640 bytes

INICIO_VOZ_MS = 160                         # cuánta voz seguida hace falta para empezar un tramo
SILENCIO_FIN_MS = 700                       # cuánto silencio cierra el tramo
PRE_ROLL_MS = 240                           # lo que se guarda de antes del primer «sí, es voz»
TRAMO_MAX_S = 45                            # un tramo no pasa de esto (45 s de WAV ≈ 1,4 MB: entra en nginx)

INICIO_VOZ_FRAMES = INICIO_VOZ_MS // FRAME_MS
SILENCIO_FIN_FRAMES = SILENCIO_FIN_MS // FRAME_MS
PRE_ROLL_FRAMES = PRE_ROLL_MS // FRAME_MS
TRAMO_MAX_FRAMES = TRAMO_MAX_S * 1000 // FRAME_MS

LATIDO_S = 55                               # cada cuánto el equipo le dice al servidor que sigue vivo
ESPERA_PIN_S = 900                          # un PIN rechazado no se vuelve a probar por 15 minutos


def wav_de(pcm: bytes) -> bytes:
    """Un WAV mono de 16 bits a 16 kHz con ese PCM crudo."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CANALES)
        w.setsampwidth(ANCHO)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


# ── La cuenta: de los cuadros de audio a los tramos con voz ──────────────────

class Segmentador:
    """Junta los cuadros (20 ms) en tramos de voz. Se le pasa, por cuadro, si
    es voz o no (afuera lo decide webrtcvad) y la hora de reloj del cuadro.
    Devuelve un tramo cuando se cierra (por silencio o por llegar al tope)."""

    def __init__(self):
        self.en_voz = False
        self.buffer: list[bytes] = []
        self.pre: list[bytes] = []          # los últimos cuadros, para el pre-roll
        self.voz_seguida = 0
        self.silencio = 0
        self.desde = 0.0

    def _cerrar(self) -> dict:
        tramo = {"pcm": b"".join(self.buffer), "dur": round(len(self.buffer) * FRAME_MS / 1000.0, 2),
                 "desde": self.desde}
        self.en_voz, self.buffer, self.voz_seguida, self.silencio = False, [], 0, 0
        return tramo

    def cuadro(self, pcm: bytes, es_voz: bool, t: float) -> dict | None:
        """Un cuadro de FRAME_BYTES. Devuelve un tramo si se acaba de cerrar."""
        if not self.en_voz:
            self.pre.append(pcm)
            if len(self.pre) > PRE_ROLL_FRAMES:
                self.pre.pop(0)
            self.voz_seguida = self.voz_seguida + 1 if es_voz else 0
            if self.voz_seguida >= INICIO_VOZ_FRAMES:
                self.en_voz, self.silencio = True, 0
                self.buffer = list(self.pre)
                self.desde = t - len(self.buffer) * FRAME_MS / 1000.0
                self.pre = []
            return None
        self.buffer.append(pcm)
        self.silencio = 0 if es_voz else self.silencio + 1
        if self.silencio >= SILENCIO_FIN_FRAMES or len(self.buffer) >= TRAMO_MAX_FRAMES:
            return self._cerrar()
        return None

    def cortar(self) -> dict | None:
        """Lo que haya en curso, cerrado a la fuerza (al apagar o al cambiar el día)."""
        return self._cerrar() if self.en_voz and self.buffer else None


# ── Lo que se manda al servidor ──────────────────────────────────────────────

def _multipart(campos: dict, nombre: str, datos: bytes, tipo: str) -> tuple:
    """Un cuerpo multipart/form-data con los campos y el archivo. Devuelve
    (cuerpo, content-type)."""
    borde = "----autcalaequipo" + secrets.token_hex(8)
    cuerpo = bytearray()
    for k, v in campos.items():
        cuerpo += (f"--{borde}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8")
    cuerpo += (f"--{borde}\r\nContent-Disposition: form-data; name=\"archivo\"; filename=\"{nombre}\"\r\n"
               f"Content-Type: {tipo}\r\n\r\n").encode("utf-8")
    cuerpo += datos + f"\r\n--{borde}--\r\n".encode("utf-8")
    return bytes(cuerpo), f"multipart/form-data; boundary={borde}"


class Caja:
    """Entra con el PIN, abre el día y manda los tramos de audio, igual que la
    caja de la PC. Un PIN rechazado no se vuelve a probar enseguida: trabaría
    la caja del local."""

    def __init__(self, api: str, local_id: str, pin: str, pedir=None):
        self.api, self.local_id, self.pin = api.rstrip("/"), local_id, pin
        self.token, self.turno, self.no_antes, self.apagado = "", "", 0.0, False
        self.pedir = pedir or self._pedir

    @staticmethod
    def _pedir(url: str, cuerpo: bytes, cabeceras: dict) -> tuple:
        pedido = urllib.request.Request(url, data=cuerpo, method="POST",
                                        headers={"User-Agent": "autcala-equipo-oido/1", **cabeceras})
        try:
            with urllib.request.urlopen(pedido, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, {}

    def _json(self, ruta: str, cuerpo: dict, con_token: bool) -> tuple:
        cab = {"Content-Type": "application/json"}
        if con_token:
            cab["X-Caja-Token"] = self.token
        return self.pedir(f"{self.api}{ruta}", json.dumps(cuerpo).encode("utf-8"), cab)

    def _entrar(self, ahora: float) -> bool:
        if ahora < self.no_antes:
            return False
        codigo, r = self._json(f"/comercios/caja/{self.local_id}/entrar", {"pin": self.pin}, False)
        if codigo == 200 and r.get("token"):
            self.token = r["token"]
            return True
        if codigo in (401, 429):
            self.no_antes = ahora + ESPERA_PIN_S
            log.error("el servidor no aceptó el PIN (%s): vuelvo a probar en 15 minutos", codigo)
        else:
            log.warning("no pude entrar (%s)", codigo)
        return False

    def _abrir(self, ahora: float) -> bool:
        codigo, r = self._json("/comercios/caja/turno/abrir", {"cajero": ""}, True)
        if codigo == 401:
            self.token = ""
            if not self._entrar(ahora):
                return False
            codigo, r = self._json("/comercios/caja/turno/abrir", {"cajero": ""}, True)
        turno = (r.get("turno") or {}).get("id") if codigo == 200 else None
        if turno:
            self.turno = turno
            return True
        log.warning("no pude abrir el día (%s)", codigo)
        return False

    def _listo(self, ahora: float) -> bool:
        if self.apagado:
            return False
        if not self.token and not self._entrar(ahora):
            return False
        if not self.turno and not self._abrir(ahora):
            return False
        return True

    def mandar_tramo(self, pcm: bytes, dur: float, desde: float, ahora: float) -> bool:
        """Un tramo de voz al oído del servidor. True si lo guardó."""
        if not self._listo(ahora):
            return False
        campos = {"duracion_s": f"{dur:.2f}",
                  "capturado_en": datetime.fromtimestamp(desde, timezone.utc).isoformat()}
        cuerpo, tipo = _multipart(campos, "tramo.wav", wav_de(pcm), "audio/wav")
        url = f"{self.api}/comercios/caja/turno/{self.turno}/oido"
        codigo, _ = self.pedir(url, cuerpo, {"Content-Type": tipo, "X-Caja-Token": self.token})
        if codigo == 401:                    # el token venció (dura 30 h): se entra de nuevo
            self.token = self.turno = ""
            if not self._listo(ahora):
                return False
            cuerpo, tipo = _multipart(campos, "tramo.wav", wav_de(pcm), "audio/wav")
            codigo, _ = self.pedir(f"{self.api}/comercios/caja/turno/{self.turno}/oido", cuerpo,
                                   {"Content-Type": tipo, "X-Caja-Token": self.token})
        if codigo == 409:                    # el día se cerró (cambió de día): se abre el nuevo la próxima
            self.turno = ""
            return False
        if codigo == 403:                    # el oído del servidor no está prendido para este local
            self.apagado = True
            log.error("el oído del servidor no está prendido para este local: no mando más audio")
            return False
        if codigo == 413:                    # el tramo quedó grande: se descarta, no se reintenta
            log.warning("un tramo quedó demasiado grande y se descartó")
            return True
        if codigo == 200:
            return True
        log.warning("el servidor contestó %s al tramo", codigo)
        return False

    def latido(self, ahora: float) -> None:
        """Cada tanto, decir que el equipo sigue oyendo, para que el servidor no
        avise «la caja dejó de responder». No pelea la caja con la PC (sin
        `pestana`): sólo mantiene el día vivo."""
        if not self._listo(ahora):
            return
        codigo, _ = self._json(f"/comercios/caja/turno/{self.turno}/latido",
                               {"escuchando": True, "detector": "servidor", "mic": "ok"}, True)
        if codigo == 409:
            self.turno = ""
        elif codigo == 401:
            self.token = self.turno = ""


class SoloOir:
    """Para probar en casa (MANDAR=no): no manda nada. Escribe en el registro
    lo que oyó y lo da por enviado."""

    def mandar_tramo(self, pcm: bytes, dur: float, desde: float, ahora: float) -> bool:
        log.info("oí un tramo de %.1f s (no lo mando: MANDAR=no)", dur)
        return True

    def latido(self, ahora: float) -> None:
        pass


# ── La red: el audio de la cámara ────────────────────────────────────────────

def url_rtsp() -> str:
    """La dirección RTSP de la cámara para el audio. Usa el canal OIDO_RUTA
    (por defecto /stream1, el de mejor audio), con el usuario y la clave del
    .env (ya vienen codificados para la URL)."""
    usuario = os.environ.get("FRIGATE_RTSP_USER", "")
    clave = os.environ.get("FRIGATE_RTSP_PASSWORD", "")
    ip = os.environ.get("FRIGATE_CAMARA_IP", "")
    puerto = os.environ.get("FRIGATE_CAMARA_PUERTO", "554")
    ruta = os.environ.get("OIDO_RUTA", "/stream1")
    cred = f"{usuario}:{clave}@" if usuario else ""
    return f"rtsp://{cred}{ip}:{puerto}{ruta}"


# El filtro de ffmpeg que limpia el ruido ANTES del detector de voz y de
# Whisper. Con OIDO_DENOISE vacío o "no" no toca nada (como hasta ahora). "si"
# aplica un filtro razonable para un local ruidoso: corta el retumbe grave y un
# denoiser FFT que sigue el ruido de fondo. También acepta un filtro de ffmpeg a
# medida (p. ej. `arnndn=m=modelo.rnnn` con un modelo RNNoise, o sumarle
# `,dynaudnorm` para levantar una voz floja). Ayuda cuando la voz está tapada por
# ruido; no inventa una voz que llegó demasiado débil.
DENOISE_DEFECTO = "highpass=f=150,afftdn=nf=-25:tn=1"


def _filtro_denoise() -> str:
    v = os.environ.get("OIDO_DENOISE", "").strip()
    if not v or v.lower() in ("no", "0", "false"):
        return ""
    if v.lower() in ("si", "sí", "1", "true"):
        return DENOISE_DEFECTO
    return v


def _entrada() -> list:
    """Los argumentos de ffmpeg para la FUENTE del audio: la cámara por RTSP
    (OIDO_FUENTE=rtsp, por defecto) o el micrófono de una PC Windows
    (OIDO_FUENTE=mic, con OIDO_MIC = el nombre del micrófono)."""
    if os.environ.get("OIDO_FUENTE", "rtsp").strip().lower() == "mic":
        mic = os.environ.get("OIDO_MIC", "").strip()
        if not mic:
            raise SystemExit("Falta OIDO_MIC: el nombre del micrófono. Listalos con:\n"
                             "  ffmpeg -hide_banner -list_devices true -f dshow -i dummy")
        return ["-f", "dshow", "-i", "audio=" + mic]
    return ["-rtsp_transport", "tcp", "-i", url_rtsp()]


def capturar():
    """Los cuadros de audio, de a FRAME_BYTES, con ffmpeg (de la cámara o del
    micrófono de la PC, según OIDO_FUENTE). Corta cuando ffmpeg se cae; main()
    lo vuelve a arrancar."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"] + _entrada() + ["-vn"]
    filtro = _filtro_denoise()
    if filtro:
        cmd += ["-af", filtro]
    cmd += ["-ac", str(CANALES), "-ar", str(RATE), "-f", "s16le", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    try:
        while True:
            datos = proc.stdout.read(FRAME_BYTES)
            if len(datos) < FRAME_BYTES:
                return
            yield datos
    finally:
        proc.kill()
        proc.wait()


def main() -> None:
    import webrtcvad   # sólo hace falta acá: las pruebas no lo usan

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # La cámara como oído es una decisión aparte (OIDO=si): si está apagado, no
    # toca el audio, para no chocar con el micrófono de la PC del local.
    if os.environ.get("OIDO", "no").strip().lower() not in ("si", "sí", "1", "true"):
        log.info("el oído por la cámara está apagado (OIDO=no). No capturo audio.")
        while True:
            time.sleep(3600)
    solo = os.environ.get("MANDAR", "si").strip().lower() in ("no", "0", "false")
    falta = [k for k in ("LOCAL_ID", "PIN") if not os.environ.get(k)]
    if falta and not solo:
        raise SystemExit("Falta en el .env: " + ", ".join(falta) + " (o poné MANDAR=no para probar sin mandar)")
    por_mic = os.environ.get("OIDO_FUENTE", "rtsp").strip().lower() == "mic"
    if por_mic and not os.environ.get("OIDO_MIC"):
        raise SystemExit("Falta OIDO_MIC: el nombre del micrófono (ffmpeg -list_devices true -f dshow -i dummy)")
    if not por_mic and not os.environ.get("FRIGATE_CAMARA_IP"):
        raise SystemExit("Falta la cámara: corré ./instalar.sh --camara")

    caja = SoloOir() if solo else Caja(os.environ.get("API", "https://api-bis.autcala.com/api/v1"),
                                       os.environ["LOCAL_ID"], os.environ["PIN"])
    vad = webrtcvad.Vad(int(os.environ.get("OIDO_VAD", "2")))
    seg = Segmentador()
    fuente = "el micrófono de la PC" if por_mic else "el canal " + os.environ.get("OIDO_RUTA", "/stream1")
    log.info("oído %s, por %s", "de prueba: NO manda nada" if solo else "del local " + os.environ["LOCAL_ID"], fuente)
    if _filtro_denoise():
        log.info("limpieza de ruido activada (OIDO_DENOISE): %s", _filtro_denoise())
    prox_latido = 0.0
    while True:
        try:
            for cuadro in capturar():
                ahora = time.time()
                tramo = seg.cuadro(cuadro, vad.is_speech(cuadro, RATE), ahora)
                if tramo:
                    caja.mandar_tramo(tramo["pcm"], tramo["dur"], tramo["desde"], ahora)
                if ahora >= prox_latido:
                    caja.latido(ahora)
                    prox_latido = ahora + LATIDO_S
        except Exception as e:  # noqa: BLE001
            log.error("se cortó la captura (%s); reintento en 5 s", type(e).__name__)
        time.sleep(5)


if __name__ == "__main__":
    main()
