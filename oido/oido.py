# -*- coding: utf-8 -*-
"""El oído del equipo: el audio del local al oído del servidor de Autcala.

El audio sale de un MICRÓFONO USB enchufado a la Pi (OIDO_FUENTE=usb), del
RTSP de una cámara (rtsp) o del micrófono de una PC Windows (mic), y va en
tramos al mismo oído del servidor que usa la tablet del mostrador
(`/caja/turno/{id}/oido`). El servidor los junta por ventanas, los transcribe
con Whisper y arma el resumen de cada ticket. De acá no sale texto ni video, y
el audio no se guarda en ningún lado: vive en la memoria lo que tarda en
subir, y si no puede subir en unos minutos se tira.

Dos maneras de cortar el audio en tramos (OIDO_MODO):
  - «corrido» (la de la tablet desde la 124b, y la de fábrica con el micrófono
    USB): se manda todo, en tramos de 45 s, y decide Whisper. En el local se
    midió que es mucho mejor que mandar sólo lo que un detector cree que es voz.
  - «voz»: sólo los tramos con voz según webrtcvad (la primera versión, para
    la cámara).

Como en la tablet, el audio se amplifica según la sensibilidad que el dueño
elige en su panel (Máxima, Alta, Normal), con un tope para que una voz pegada
al micrófono no sature. El equipo la vuelve a preguntar cada cinco minutos.

La subida va en un hilo aparte: si la red tarda, la grabación no se corta.

Cada tramo va como WAV mono, 16 bits, 16 kHz, que es lo que el servidor pide
(si no coincide, lo descarta). El equipo abre el «día» de la caja con el PIN,
igual que la tablet, y lo mantiene vivo con un latido por minuto.

La cuenta (Segmentador, Corrido, realzar), el trato con el servidor (Caja) y la
cola de subida (Mensajero) no dependen de ffmpeg, de la red ni de una
biblioteca de voz: se prueban con audio y respuestas de mentira
(test_oido.py). ffmpeg y webrtcvad se usan sólo en main().
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from array import array
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("oido")

VERSION = "equipo-oido-2026-11-26"

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
SENSIBILIDAD_S = 300                        # cada cuánto se pregunta la sensibilidad que eligió el dueño
COLA_MAX = 4                                # tramos esperando subir (unos 3 minutos); de ahí, se tira el más viejo
REINTENTO_S = 5                             # si la red falló, cuánto se espera para reintentar
SIN_MIC_S = 30                              # sin micrófono, cada cuánto se vuelve a buscar

# Lo que el servidor contesta cuando el problema es de la red o de él: se reintenta.
REINTENTAR = (0, 429, 500, 502, 503, 504)


def wav_de(pcm: bytes) -> bytes:
    """Un WAV mono de 16 bits a 16 kHz con ese PCM crudo."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CANALES)
        w.setsampwidth(ANCHO)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def nivel_db(pcm: bytes) -> float:
    """El nivel de un pedazo de audio, en dB (0 = lo más fuerte que entra; -120 = silencio)."""
    m = array("h")
    m.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
    if not m:
        return -120.0
    rms = math.sqrt(sum(x * x for x in m) / len(m)) / 32768.0
    return max(-120.0, 20 * math.log10(rms)) if rms > 0 else -120.0


# ── La amplificación: la misma idea que la cadena natural de la tablet ───────

# Cuánto amplifica como mucho según la sensibilidad del panel (el umbral que
# manda el servidor): la misma cuenta que la tablet (gananciaNaturalDe):
# ×24 en Máxima, ×12 en Alta, ×6 en Normal.
def ganancia_de(umbral: float) -> float:
    return 24.0 if umbral <= 0.35 else 12.0 if umbral <= 0.5 else 6.0


UMBRAL_DE_FABRICA = 0.5                     # Alta, hasta que el servidor diga otra cosa
TOPE = 16422                                # −6 dB: de acá para arriba, el tope (el de la tablet, TOPE_B)
TOPE_RAZON = 20


def realzar(pcm: bytes, gmax: float) -> tuple:
    """El tramo amplificado. Devuelve (el audio, la ganancia que se usó).

    En la tablet la amplificación es fija porque el micrófono es siempre el
    mismo. Un micrófono USB puede venir mucho más fuerte: amplificarlo ×24
    aplastaría todo contra el tope. Por eso la ganancia se elige por tramo:
    la de la sensibilidad, salvo que con menos ya alcance para que casi todo
    el tramo (el 95 % de sus pedazos de 20 ms) quede por debajo del tope. Lo
    que igual pasa del tope (un golpe, una voz pegada) se aplasta en vez de
    romperse. Nunca baja el volumen: como mucho, lo deja como está."""
    m = array("h")
    m.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
    if not m:
        return pcm, 1.0
    paso = RATE * FRAME_MS // 1000
    picos = sorted(max(max(m[i:i + paso]), -min(m[i:i + paso])) for i in range(0, len(m), paso))
    p95 = picos[min(len(picos) - 1, int(len(picos) * 0.95))]
    g = max(1.0, min(gmax, TOPE / max(1, p95)))
    if g == 1.0:
        return pcm, 1.0
    salida = array("h", bytes(len(m) * 2))
    for i, x in enumerate(m):
        y = x * g
        a = -y if y < 0 else y
        if a > TOPE:
            a = min(32767.0, TOPE + (a - TOPE) / TOPE_RAZON)
            y = -a if y < 0 else a
        salida[i] = int(y)
    return salida.tobytes(), round(g, 2)


# ── La cadena del navegador (OIDO_CADENA) ────────────────────────────────────
#
# El mejor día del oído fue el 2026-11-12: el micrófono lo abría la web de la
# caja en el navegador de la PC, que antes de entregar el audio le pasa su
# propia limpieza (filtro de ruido y ganancia automática). Esa limpieza es una
# biblioteca abierta (la de WebRTC), y acá se le aplica la misma al audio de la
# cámara, para medir cuánto de aquel día era la limpieza y cuánto que el
# micrófono estaba al lado del cajero.
#   OIDO_CADENA=natural    lo de siempre: el audio como viene, amplificado (de fábrica)
#   OIDO_CADENA=navegador  siempre con la limpieza del navegador
#   OIDO_CADENA=turnos     media hora cada una, por el reloj: así se comparan
#                          contra los tickets, como se hizo con la tablet (124)
# La cancelación de eco del navegador no se emula: no hay parlante que cancelar.
NAVEGADOR_RUIDO = 3                         # el filtro de ruido en «alto», como lo pone Chrome (0 a 4)
NAVEGADOR_NIVEL = 3                         # la ganancia automática apunta a −3 dB del tope (0 la apaga)
BLOQUE_S = 30 * 60


def cadena() -> str:
    v = os.environ.get("OIDO_CADENA", "").strip().lower()
    return v if v in ("navegador", "turnos") else "natural"


def turno_de(t: float) -> int:
    """0 o 1: qué media hora es. La misma cuenta que el servidor (kiosco.py,
    `turno_de`); se le suma el día para que una hora no caiga siempre igual."""
    s = int(t)
    return (s // BLOQUE_S + s // 86400) % 2


def toca_navegador(cad: str, t: float) -> bool:
    """Si en ese momento se escucha con la limpieza del navegador (en `turnos`, el turno 1)."""
    return cad == "navegador" or (cad == "turnos" and turno_de(t) == 1)


class Navegador:
    """La limpieza del navegador, cuadro por cuadro. La biblioteca trabaja de a
    10 ms y los cuadros son de 20: se parten en dos. `procesar10` se puede
    reemplazar en las pruebas, que no tienen la biblioteca."""

    def __init__(self, procesar10=None):
        if procesar10 is None:
            from webrtc_noise_gain import AudioProcessor   # sólo hace falta acá
            ap = AudioProcessor(NAVEGADOR_NIVEL, NAVEGADOR_RUIDO)
            procesar10 = lambda b: ap.Process10ms(b).audio  # noqa: E731
        self.procesar10 = procesar10

    def cuadro(self, pcm: bytes) -> bytes:
        mitad = len(pcm) // 2
        return bytes(self.procesar10(pcm[:mitad])) + bytes(self.procesar10(pcm[mitad:]))


# ── La cuenta: de los cuadros de audio a los tramos ──────────────────────────

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


class Corrido:
    """De corrido: todo el audio, en tramos de TRAMO_MAX_S, sin mirar si hay
    voz (lo decide Whisper). Mismo trato que el Segmentador: `t` es la hora de
    reloj del cuadro, y la hora del tramo es la de su primer cuadro."""

    def __init__(self):
        self.buffer: list[bytes] = []
        self.desde = 0.0

    def cuadro(self, pcm: bytes, es_voz: bool, t: float) -> dict | None:
        if not self.buffer:
            self.desde = t
        self.buffer.append(pcm)
        return self._cerrar() if len(self.buffer) >= TRAMO_MAX_FRAMES else None

    def _cerrar(self) -> dict:
        tramo = {"pcm": b"".join(self.buffer), "dur": round(len(self.buffer) * FRAME_MS / 1000.0, 2),
                 "desde": self.desde}
        self.buffer = []
        return tramo

    def cortar(self) -> dict | None:
        """Lo que haya, si alcanza para que valga mandarlo (un segundo o más)."""
        if len(self.buffer) * FRAME_MS >= 1000:
            return self._cerrar()
        self.buffer = []
        return None


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
    tablet. Un PIN rechazado no se vuelve a probar enseguida: trabaría la caja
    del local. `ultimo` es el código de la última respuesta (0 = sin red)."""

    def __init__(self, api: str, local_id: str, pin: str, pedir=None, puesto: str = ""):
        self.api, self.local_id, self.pin = api.rstrip("/"), local_id, pin
        self.token, self.turno, self.no_antes, self.apagado = "", "", 0.0, False
        self.puesto = puesto
        self.ultimo = 0
        self.pedir = pedir or self._pedir

    @staticmethod
    def _pedir(url: str, cuerpo: bytes | None, cabeceras: dict) -> tuple:
        pedido = urllib.request.Request(url, data=cuerpo, method="POST" if cuerpo is not None else "GET",
                                        headers={"User-Agent": "autcala-equipo-oido/2", **cabeceras})
        try:
            with urllib.request.urlopen(pedido, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, {}
        except (urllib.error.URLError, OSError, ValueError):
            return 0, {}                     # sin red, o una respuesta rota: se reintenta más tarde

    def _json(self, ruta: str, cuerpo: dict, con_token: bool) -> tuple:
        cab = {"Content-Type": "application/json"}
        if con_token:
            cab["X-Caja-Token"] = self.token
        codigo, r = self.pedir(f"{self.api}{ruta}", json.dumps(cuerpo).encode("utf-8"), cab)
        self.ultimo = codigo
        return codigo, r

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
        codigo, r = self._json("/comercios/caja/turno/abrir", {"cajero": "equipo"}, True)
        if codigo == 401:
            self.token = ""
            if not self._entrar(ahora):
                return False
            codigo, r = self._json("/comercios/caja/turno/abrir", {"cajero": "equipo"}, True)
        turno = (r.get("turno") or {}).get("id") if codigo == 200 else None
        if turno:
            self.turno = turno
            if r.get("token"):
                self.token = r["token"]      # abrir el día trae un token nuevo, de 30 horas
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

    def _subir(self, campos: dict, pcm: bytes) -> int:
        cuerpo, tipo = _multipart(campos, "tramo.wav", wav_de(pcm), "audio/wav")
        codigo, _ = self.pedir(f"{self.api}/comercios/caja/turno/{self.turno}/oido", cuerpo,
                               {"Content-Type": tipo, "X-Caja-Token": self.token})
        self.ultimo = codigo
        return codigo

    def mandar_tramo(self, pcm: bytes, dur: float, desde: float, ahora: float) -> bool:
        """Un tramo al oído del servidor. True si lo guardó (o si no tiene
        sentido reintentarlo). Con False, `ultimo` dice si conviene reintentar
        (REINTENTAR)."""
        if not self._listo(ahora):
            return False
        campos = {"duracion_s": f"{dur:.2f}",
                  "capturado_en": datetime.fromtimestamp(desde, timezone.utc).isoformat()}
        if self.puesto:
            campos["puesto"] = self.puesto   # en un local con varios equipos, cuál es este (125)
        codigo = self._subir(campos, pcm)
        if codigo == 401:                    # el token venció: se entra de nuevo
            self.token = self.turno = ""
            if not self._listo(ahora):
                return False
            codigo = self._subir(campos, pcm)
        if codigo in (404, 409):             # el día se cerró (cambió de día): se abre el nuevo la próxima
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
        log.warning("el servidor contestó %s al tramo", codigo or "nada (¿sin internet?)")
        return False

    def sensibilidad(self, ahora: float) -> float | None:
        """El umbral de la sensibilidad que el dueño eligió en su panel (lo
        mismo que lee la tablet con la publicidad). None si no se pudo saber."""
        if not self._listo(ahora):
            return None
        codigo, r = self.pedir(f"{self.api}/comercios/caja/kiosco/anuncios", None, {"X-Caja-Token": self.token})
        self.ultimo = codigo
        if codigo == 401:
            self.token = self.turno = ""
        try:
            u = float(((r or {}).get("oido") or {}).get("umbral"))
            return u if 0.05 <= u <= 0.95 else None
        except (TypeError, ValueError):
            return None

    def latido(self, ahora: float, datos: dict | None = None) -> None:
        """Cada minuto, decir que el equipo sigue oyendo, para que el servidor
        no avise «la caja dejó de responder», con lo que le pasó al micrófono
        en ese minuto (sólo números). Si el micrófono no manda nada, lo dice
        (`mic: sin_senal`): el servidor le avisa al dueño."""
        if not self._listo(ahora):
            return
        cuerpo = {"escuchando": True, "detector": "servidor", "mic": "ok", "palabras": False,
                  "version": VERSION, **(datos or {})}
        codigo, _ = self._json(f"/comercios/caja/turno/{self.turno}/latido", cuerpo, True)
        if codigo in (404, 409):
            self.turno = ""
        elif codigo == 401:
            self.token = self.turno = ""


class SoloOir:
    """Para probar en casa (MANDAR=no): no manda nada. Escribe en el registro
    lo que oyó y lo da por enviado."""
    ultimo = 200

    def mandar_tramo(self, pcm: bytes, dur: float, desde: float, ahora: float) -> bool:
        log.info("oí un tramo de %.1f s, a %.0f dB (no lo mando: MANDAR=no)", dur, nivel_db(pcm))
        return True

    def sensibilidad(self, ahora: float) -> float | None:
        return None

    def latido(self, ahora: float, datos: dict | None = None) -> None:
        log.info("latido: %s", json.dumps(datos or {}, ensure_ascii=False))


class Mensajero:
    """La cola de subida, en memoria. La captura deja los tramos y el latido
    acá y sigue grabando; un hilo aparte los sube. Si la red se corta, se
    reintenta cada REINTENTO_S; si se juntan más de COLA_MAX tramos, se tira el
    más viejo. Nada va al disco."""

    def __init__(self, caja, reloj=time.time, gmax=None):
        self.caja, self.reloj = caja, reloj
        # Cuánto se puede amplificar como mucho (la sensibilidad del panel, u OIDO_GANANCIA).
        self.gmax = gmax or (lambda: ganancia_de(self.umbral))
        self.cola: deque = deque()
        self.latido_pendiente: dict | None = None
        self.umbral = UMBRAL_DE_FABRICA
        self.prox_sensibilidad = 0.0
        self.perdidos = 0                    # tramos tirados porque la cola se llenó
        self.errores = 0                     # subidas que fallaron (se reintentan)
        self.ganancia = 0.0                  # la del último tramo que se amplificó
        self.nivel_max = -120.0              # el nivel más alto de lo que se subió, ya amplificado
        self.lock = threading.Lock()
        self.hay = threading.Event()

    def tramo(self, t: dict) -> None:
        with self.lock:
            if len(self.cola) >= COLA_MAX:
                self.cola.popleft()
                self.perdidos += 1
            self.cola.append(t)
        self.hay.set()

    def latir(self, datos: dict) -> None:
        with self.lock:
            self.latido_pendiente = datos
        self.hay.set()

    def paso(self) -> bool:
        """Una cosa por vez: la sensibilidad si toca, el latido si hay, o el
        tramo más viejo. Devuelve True si hay que esperar antes de seguir (la
        red falló o no había nada)."""
        ahora = self.reloj()
        if ahora >= self.prox_sensibilidad:
            self.prox_sensibilidad = ahora + SENSIBILIDAD_S
            u = self.caja.sensibilidad(ahora)
            if u is not None and u != self.umbral:
                log.info("la sensibilidad del panel cambió: amplifico hasta ×%g", ganancia_de(u))
                self.umbral = u
        with self.lock:
            latido, self.latido_pendiente = self.latido_pendiente, None
        if latido is not None:
            self.caja.latido(ahora, latido)
        with self.lock:
            t = self.cola[0] if self.cola else None
        if t is None:
            return True
        if "g" not in t:
            # La amplificación se hace acá y no al grabar: lleva un rato y la grabación no espera.
            t["pcm"], t["g"] = realzar(t["pcm"], self.gmax())
            self.ganancia = t["g"]
            self.nivel_max = max(self.nivel_max, nivel_db(t["pcm"]))
        hecho = self.caja.mandar_tramo(t["pcm"], t["dur"], t["desde"], ahora)
        reintentar = not hecho and getattr(self.caja, "ultimo", 0) in REINTENTAR and not getattr(self.caja, "apagado", False)
        if reintentar:
            self.errores += 1
            return True
        with self.lock:
            if self.cola and self.cola[0] is t:
                self.cola.popleft()
        return False

    def correr(self) -> None:
        while True:
            try:
                esperar = self.paso()
            except Exception as e:  # noqa: BLE001
                log.error("la subida falló (%s)", type(e).__name__)
                esperar = True
            if esperar:
                self.hay.wait(REINTENTO_S)
                self.hay.clear()


# ── De dónde sale el audio ───────────────────────────────────────────────────

class SinMicrofono(Exception):
    pass


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


def mic_usb(tarjetas: str) -> str:
    """El primer micrófono USB de la lista de tarjetas de sonido de Linux
    (/proc/asound/cards), como lo pide ffmpeg: plughw:CARD=<nombre>,DEV=0
    (plughw convierte solo a 16 kHz y un canal). Vacío si no hay ninguno."""
    for linea in (tarjetas or "").splitlines():
        m = re.match(r"\s*\d+\s+\[(\S+)\s*\]:\s*(\S+)", linea)
        if m and "usb" in m.group(2).lower():
            return f"plughw:CARD={m.group(1)},DEV=0"
    return ""


def fuente() -> str:
    return os.environ.get("OIDO_FUENTE", "rtsp").strip().lower() or "rtsp"


def _entrada() -> list:
    """Los argumentos de ffmpeg para la FUENTE del audio: un micrófono USB en
    la Pi (OIDO_FUENTE=usb; OIDO_MIC elige uno, si no, el primero que haya),
    la cámara por RTSP (rtsp, por defecto) o el micrófono de una PC Windows
    (mic, con OIDO_MIC = el nombre del micrófono)."""
    f = fuente()
    if f == "usb":
        mic = os.environ.get("OIDO_MIC", "").strip()
        if not mic:
            try:
                with open("/proc/asound/cards", encoding="utf-8", errors="replace") as a:
                    mic = mic_usb(a.read())
            except OSError:
                mic = ""
        if not mic:
            raise SinMicrofono("no encuentro un micrófono USB enchufado")
        return ["-f", "alsa", "-i", mic]
    if f == "mic":
        mic = os.environ.get("OIDO_MIC", "").strip()
        if not mic:
            raise SystemExit("Falta OIDO_MIC: el nombre del micrófono. Listalos con:\n"
                             "  ffmpeg -hide_banner -list_devices true -f dshow -i dummy")
        return ["-f", "dshow", "-i", "audio=" + mic]
    # -timeout: si la conexión RTSP se cuelga (el Wi-Fi de la cámara es
    # inestable), ffmpeg corta a los 10 s y main() lo vuelve a arrancar, en vez
    # de quedar colgado para siempre.
    return ["-rtsp_transport", "tcp", "-timeout", "10000000", "-i", url_rtsp()]


# El filtro de ffmpeg que limpia el ruido. Con OIDO_DENOISE vacío o "no" no
# toca nada. OJO: en el local, limpiar el audio antes de Whisper le jugó en
# contra (la prueba de DeepFilterNet, 2026-11-23: casi sin texto). Queda para
# probar, apagado.
DENOISE_DEFECTO = "highpass=f=150,afftdn=nf=-25:tn=1"


def _filtro_denoise() -> str:
    v = os.environ.get("OIDO_DENOISE", "").strip()
    if not v or v.lower() in ("no", "0", "false"):
        return ""
    if v.lower() in ("si", "sí", "1", "true"):
        return DENOISE_DEFECTO
    return v


def capturar():
    """Los cuadros de audio, de a FRAME_BYTES, con ffmpeg. Corta cuando ffmpeg
    se cae (se desenchufó el micrófono, se cortó la cámara); main() lo vuelve
    a arrancar."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"] + _entrada() + ["-vn"]
    filtro = _filtro_denoise()
    if filtro:
        cmd += ["-af", filtro]
    cmd += ["-ac", str(CANALES), "-ar", str(RATE), "-f", "s16le", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    try:
        yield from cuadros_de(proc.stdout.read)
    finally:
        proc.kill()
        proc.wait()


def cuadros_de(leer):
    """Los cuadros enteros, de a FRAME_BYTES, de lo que va entregando `leer`.
    Un pipe puede entregar menos de lo pedido sin que se haya terminado: se
    junta hasta completar el cuadro. Termina cuando `leer` ya no da nada."""
    resto = b""
    while True:
        datos = leer(FRAME_BYTES - len(resto))
        if not datos:
            return
        resto += datos
        if len(resto) == FRAME_BYTES:
            yield resto
            resto = b""


# ── Lo que pasó en el minuto, para el latido ────────────────────────────────

class Minuto:
    """Lo que le pasó al micrófono desde el latido anterior. Sólo números."""

    def __init__(self):
        self.cuadros = self.tramos = 0
        self.segundos = 0.0
        self.crudo_max = -120.0

    def para_latido(self, mensajero: Mensajero | None, variante: int) -> dict:
        errores = perdidos = 0
        umbral, ganancia, nivel = UMBRAL_DE_FABRICA, 0.0, -120.0
        if mensajero is not None:
            errores, perdidos, umbral = mensajero.errores, mensajero.perdidos, mensajero.umbral
            ganancia, nivel = mensajero.ganancia, mensajero.nivel_max
            mensajero.errores = mensajero.perdidos = 0
            mensajero.nivel_max = -120.0
        # Sin un solo cuadro en todo el minuto, o con silencio digital puro (todo
        # ceros), el micrófono está desenchufado o muteado. Un corte de un
        # segundo (ffmpeg que se reabre) no alcanza para avisar.
        oye = self.cuadros > 0 and self.crudo_max > -119
        return {
            "escuchando": True, "mic": "ok" if oye else "sin_senal", "voz": self.tramos > 0,
            "nivel": round(nivel) if nivel > -120 else None, "voz_s": round(self.segundos),
            "vosk_diag": {"cuadros": self.cuadros, "tramos": self.tramos, "segundos": round(self.segundos),
                          "errores": errores, "perdidos": perdidos, "nivel_max_db": round(nivel),
                          "crudo_max": round(self.crudo_max), "umbral": round(umbral * 100),
                          # 1 = la natural, 2 = con la limpieza del navegador (filtro de ruido y ganancia).
                          "variante": variante, "ns": int(variante == 2), "ec": 0, "agc": int(variante == 2),
                          "ganancia": round(ganancia * 10)},   # la del último tramo, por diez
        }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # El oído es una decisión aparte (OIDO=si): si está apagado, no toca el audio.
    if os.environ.get("OIDO", "no").strip().lower() not in ("si", "sí", "1", "true"):
        log.info("el oído está apagado (OIDO=no). No capturo audio.")
        while True:
            time.sleep(3600)
    solo = os.environ.get("MANDAR", "si").strip().lower() in ("no", "0", "false")
    falta = [k for k in ("LOCAL_ID", "PIN") if not os.environ.get(k)]
    if falta and not solo:
        raise SystemExit("Falta en el .env: " + ", ".join(falta) + " (o poné MANDAR=no para probar sin mandar)")
    f = fuente()
    if f == "mic" and not os.environ.get("OIDO_MIC"):
        raise SystemExit("Falta OIDO_MIC: el nombre del micrófono (ffmpeg -list_devices true -f dshow -i dummy)")
    if f == "rtsp" and not os.environ.get("FRIGATE_CAMARA_IP"):
        raise SystemExit("Falta la cámara: corré ./instalar.sh --camara (o usá un micrófono USB: ./instalar.sh --oido)")
    modo = (os.environ.get("OIDO_MODO", "").strip().lower() or ("corrido" if f == "usb" else "voz"))
    corrido = modo != "voz"
    fija = os.environ.get("OIDO_GANANCIA", "").strip()

    caja = SoloOir() if solo else Caja(os.environ.get("API", "https://api-bis.autcala.com/api/v1"),
                                       os.environ["LOCAL_ID"], os.environ["PIN"],
                                       puesto=os.environ.get("OIDO_PUESTO", "").strip().lower())
    def gmax() -> float:
        try:
            return max(1.0, min(50.0, float(fija)))
        except ValueError:
            return ganancia_de(mensajero.umbral)

    mensajero = Mensajero(caja, gmax=gmax)
    threading.Thread(target=mensajero.correr, name="subida", daemon=True).start()
    vad = None
    if not corrido:
        import webrtcvad   # sólo hace falta acá: las pruebas no lo usan
        vad = webrtcvad.Vad(int(os.environ.get("OIDO_VAD", "2")))
    nombres = {"usb": "un micrófono USB", "mic": "el micrófono de la PC", "rtsp": "la cámara"}
    log.info("oído %s, por %s, %s", "de prueba: NO manda nada" if solo else "del local " + os.environ["LOCAL_ID"],
             nombres.get(f, f), "de corrido en tramos de 45 s" if corrido else "sólo lo que suena a voz")
    if _filtro_denoise():
        log.info("limpieza de ruido activada (OIDO_DENOISE): %s", _filtro_denoise())
    cad = cadena()
    nav = None
    if cad != "natural":
        try:
            nav = Navegador()
            log.info("cadena de audio: %s", "la limpieza del navegador, siempre" if cad == "navegador"
                     else "media hora natural y media hora con la limpieza del navegador")
        except Exception as e:  # noqa: BLE001
            log.error("no pude prender la limpieza del navegador (%s): sigo con la natural", type(e).__name__)
            cad = "natural"

    minuto = Minuto()
    prox_latido = 0.0
    limpia = False                          # si ahora se escucha con la limpieza del navegador

    def latir(ahora: float) -> None:
        nonlocal minuto, prox_latido
        if ahora < prox_latido:
            return
        mensajero.latir(minuto.para_latido(mensajero, 2 if limpia else 1 if corrido else 0))
        minuto = Minuto()
        prox_latido = ahora + LATIDO_S

    while True:
        seg = Corrido() if corrido else Segmentador()
        espera = REINTENTO_S
        try:
            for cuadro in capturar():
                ahora = time.time()
                minuto.cuadros += 1
                db = nivel_db(cuadro)
                if db > minuto.crudo_max:
                    minuto.crudo_max = db
                if nav is not None:
                    # Se limpia siempre, también cuando no toca usarlo: así el filtro llega
                    # a su media hora ya acostumbrado al ruido del local.
                    limpio = nav.cuadro(cuadro)
                    toca = toca_navegador(cad, ahora)
                    if toca != limpia:
                        # Al cambiar de cadena se cierra el tramo: ninguno mezcla las dos.
                        limpia = toca
                        resto = seg.cortar()
                        if resto:
                            mensajero.tramo(resto)
                    if limpia:
                        cuadro = limpio
                # El reloj del cuadro: ahora, menos lo que dura (llega apenas termina).
                tramo = seg.cuadro(cuadro, vad.is_speech(cuadro, RATE) if vad else True, ahora - FRAME_MS / 1000.0)
                if tramo:
                    minuto.tramos += 1
                    minuto.segundos += tramo["dur"]
                    mensajero.tramo(tramo)
                latir(ahora)
            log.warning("se cortó el audio; lo vuelvo a abrir")
        except SinMicrofono as e:
            log.error("%s: vuelvo a buscar en %d s", e, SIN_MIC_S)
            espera = SIN_MIC_S
        except Exception as e:  # noqa: BLE001
            log.error("se cortó la captura (%s); reintento en %d s", type(e).__name__, REINTENTO_S)
        resto = seg.cortar()
        if resto:
            mensajero.tramo(resto)
        latir(time.time())
        time.sleep(espera)


if __name__ == "__main__":
    main()
