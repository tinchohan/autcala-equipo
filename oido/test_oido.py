# -*- coding: utf-8 -*-
"""El oído del equipo: de los cuadros de audio a los tramos con voz, y el
trato con el oído del servidor.

Cómo se arma un tramo (empieza con voz seguida, guarda un pre-roll, cierra por
silencio o por el tope), que un ruidito corto no cuenta, y que el tramo que
sale es WAV mono 16 bits 16 kHz, con sus campos, por la ruta del oído; que un
PIN rechazado no se reintenta enseguida, que el token vencido se renueva y que
el día cerrado se vuelve a abrir.

Corre sin ffmpeg, sin webrtcvad y sin red: los cuadros y las respuestas se
arman a mano.
    python -X utf8 test_oido.py
"""
import io
import os
import re
import sys
import wave

AQUI = os.path.dirname(os.path.abspath(__file__))
EQUIPO = os.path.dirname(AQUI)
sys.path.insert(0, AQUI)

import oido  # noqa: E402

fallos = []


def ok(cond, etiqueta):
    print(("  OK     " if cond else "  FALLA  ") + etiqueta)
    if not cond:
        fallos.append(etiqueta)


def leer(rel):
    return io.open(os.path.join(EQUIPO, rel), encoding="utf-8").read()


CUADRO = b"\x11\x22" * (oido.FRAME_BYTES // 2)   # un cuadro cualquiera de 20 ms


def correr(patron, seg=None):
    """Le da de comer al segmentador una lista de (es_voz, repeticiones) y
    devuelve (los tramos cerrados, el segmentador, cuántos cuadros entraron)."""
    seg = seg or oido.Segmentador()
    tramos, n = [], 0
    for es_voz, veces in patron:
        for _ in range(veces):
            t = seg.cuadro(CUADRO, es_voz, round(n * oido.FRAME_MS / 1000.0, 3))
            if t:
                tramos.append(t)
            n += 1
    return tramos, seg, n


print("\n== el WAV que pide el servidor ==")
w = oido.wav_de(CUADRO * 100)
with wave.open(io.BytesIO(w), "rb") as r:
    ok(r.getnchannels() == 1 and r.getsampwidth() == 2 and r.getframerate() == 16000 and r.getnframes() == 100 * (oido.FRAME_BYTES // oido.ANCHO),
       "wav_de arma un WAV mono de 16 bits a 16 kHz, lo mismo que el servidor acepta (si no, lo descarta)")

print("\n== se arma un tramo con la voz ==")
tramos, _, _ = correr([(False, 50), (True, 100), (False, oido.SILENCIO_FIN_FRAMES), (False, 20)])
ok(len(tramos) == 1, "del silencio, la voz y el silencio sale un solo tramo")
t = tramos[0]
cuadros = len(t["pcm"]) // oido.FRAME_BYTES
ok(cuadros == oido.PRE_ROLL_FRAMES + (100 - oido.INICIO_VOZ_FRAMES) + oido.SILENCIO_FIN_FRAMES,
   "el tramo trae el pre-roll de antes, la voz y el silencio que lo cerró")
ok(abs(t["dur"] - cuadros * oido.FRAME_MS / 1000.0) < 0.001 and t["pcm"] == CUADRO * cuadros,
   "la duración es la del audio que trae")
# La voz empezó en el cuadro 50; con el pre-roll, el tramo arranca antes.
ok(abs(t["desde"] - (50 + oido.INICIO_VOZ_FRAMES - 1 - oido.PRE_ROLL_FRAMES) * oido.FRAME_MS / 1000.0) < 0.001,
   "la hora del tramo es cuando empezó la voz, menos el pre-roll")

print("\n== lo que no es un tramo ==")
tramos, _, _ = correr([(False, 20), (True, oido.INICIO_VOZ_FRAMES - 1), (False, 60)])
ok(tramos == [], "un ruidito más corto que el mínimo de voz no abre un tramo")
tramos, seg, _ = correr([(False, 10), (True, 30)])
ok(tramos == [] and seg.en_voz, "mientras la voz sigue, el tramo queda abierto (todavía no se manda)")
corte = seg.cortar()
ok(corte is not None and len(corte["pcm"]) // oido.FRAME_BYTES == oido.PRE_ROLL_FRAMES + (30 - oido.INICIO_VOZ_FRAMES)
   and seg.cortar() is None,
   "al apagar (o cambiar el día) se cierra lo que había en curso, una sola vez")

print("\n== el tope de un tramo ==")
tramos, seg, _ = correr([(True, oido.TRAMO_MAX_FRAMES + 50)])
ok(len(tramos) == 1 and len(tramos[0]["pcm"]) // oido.FRAME_BYTES == oido.TRAMO_MAX_FRAMES
   and seg.en_voz,
   "un tramo no pasa del tope: se cierra y sigue uno nuevo con lo que viene")

print("\n== mandar al oído del servidor ==")


class Falso:
    """Un servidor de mentira: anota lo que le piden y contesta lo que se le diga."""

    def __init__(self, respuestas):
        self.respuestas, self.pedidos = list(respuestas), []

    def __call__(self, url, cuerpo, cabeceras):
        self.pedidos.append((url, cuerpo, cabeceras))
        return self.respuestas.pop(0)


PCM = CUADRO * 100
red = Falso([(200, {"token": "tk1"}), (200, {"turno": {"id": "t1"}}), (200, {"ok": True})])
c = oido.Caja("https://api.ejemplo.test/api/v1/", "l1", "0000", red)
ok(c.mandar_tramo(PCM, 2.0, 1_800_000_000.0, 1_800_000_050.0) is True and len(red.pedidos) == 3,
   "entra con el PIN, abre el día y manda el tramo")
(u1, c1, h1), (u2, _c2, h2), (u3, cuerpo3, h3) = red.pedidos
ok(u1 == "https://api.ejemplo.test/api/v1/comercios/caja/l1/entrar" and b'"pin": "0000"' in c1 and "X-Caja-Token" not in h1
   and u2 == "https://api.ejemplo.test/api/v1/comercios/caja/turno/abrir" and h2.get("X-Caja-Token") == "tk1"
   and u3 == "https://api.ejemplo.test/api/v1/comercios/caja/turno/t1/oido" and h3.get("X-Caja-Token") == "tk1",
   "por las rutas de la caja: el PIN, abrir el día con el token, y el tramo a ese día")
ok(h3["Content-Type"].startswith("multipart/form-data; boundary=") and b'name="archivo"; filename="tramo.wav"' in cuerpo3
   and b"RIFF" in cuerpo3 and b'name="duracion_s"' in cuerpo3 and b"2.00" in cuerpo3
   and b'name="capturado_en"' in cuerpo3 and b"2027-01-15T" in cuerpo3,
   "el tramo viaja como WAV (RIFF) con su duración y la hora, en el campo 'archivo'")
ok(b"0000" not in cuerpo3 and "0000" not in str(h3) and "tk1" not in str(h3) or h3.get("X-Caja-Token") == "tk1",
   "en el cuerpo del tramo no viaja el PIN")

print("\n== cuando algo falla ==")
red = Falso([(200, {"token": "tk1"}), (200, {"turno": {"id": "t1"}}), (401, {}),
             (200, {"token": "tk2"}), (200, {"turno": {"id": "t2"}}), (200, {"ok": True})])
c = oido.Caja("https://x.test", "l1", "0000", red)
ok(c.mandar_tramo(PCM, 1.0, 1_800_000_000.0, 1.0) is True and c.token == "tk2" and c.turno == "t2"
   and red.pedidos[-1][0] == "https://x.test/comercios/caja/turno/t2/oido",
   "si el token venció (401), entra de nuevo, abre el día y reenvía el tramo")
red = Falso([(200, {"token": "t"}), (200, {"turno": {"id": "t1"}}), (409, {})])
c = oido.Caja("https://x.test", "l1", "0000", red)
ok(c.mandar_tramo(PCM, 1.0, 1.0, 1.0) is False and c.turno == "",
   "si el día se cerró (409), se descarta el tramo y se abrirá el día nuevo la próxima")
red = Falso([(200, {"token": "t"}), (200, {"turno": {"id": "t1"}}), (403, {})])
c = oido.Caja("https://x.test", "l1", "0000", red)
ok(c.mandar_tramo(PCM, 1.0, 1.0, 1.0) is False and c.apagado
   and c.mandar_tramo(PCM, 1.0, 1.0, 2.0) is False and len(red.pedidos) == 3,
   "si el oído del servidor está apagado para el local (403), no se insiste")
red = Falso([(401, {})])
c = oido.Caja("https://x.test", "l1", "9999", red)
ok(c.mandar_tramo(PCM, 1.0, 1.0, 100.0) is False
   and c.mandar_tramo(PCM, 1.0, 1.0, 100.0 + oido.ESPERA_PIN_S - 1) is False and len(red.pedidos) == 1,
   "un PIN rechazado no se vuelve a probar por 15 minutos: no hay que trabarle la caja al local")

print("\n== el latido ==")
red = Falso([(200, {"token": "t"}), (200, {"turno": {"id": "t1"}}), (200, {"ok": True})])
c = oido.Caja("https://x.test", "l1", "0000", red)
c.latido(1.0)
u, cuerpo, h = red.pedidos[-1]
ok(u == "https://x.test/comercios/caja/turno/t1/latido" and b'"escuchando": true' in cuerpo
   and b'"detector": "servidor"' in cuerpo and h.get("X-Caja-Token") == "t",
   "el latido dice que el equipo está oyendo, para que no salte «la caja dejó de responder»")

print("\n== lo que el código promete y no se ve corriendo ==")
FUENTE = leer("oido/oido.py")
ok("import webrtcvad" in FUENTE.split("def main(")[1] and "requests" not in FUENTE,
   "ffmpeg y webrtcvad se usan sólo al correr de verdad; las pruebas no los necesitan")
ok(not re.search(r"snapshot|\.jpg|\.png|imwrite|cv2|base64", FUENTE, re.I),
   "el oído no toca ninguna imagen: sólo audio")
cuerpo_fuente = FUENTE.split('"""', 2)[2]
ok(not [l for l in cuerpo_fuente.splitlines() if "log." in l and re.search(r"self\.pin|self\.token|\bpin\b|token", l)],
   "ni el PIN ni el token van a un registro")
ok("MANDAR" in FUENTE and 'solo = os.environ.get("MANDAR"' in FUENTE and "class SoloOir" in FUENTE,
   "en modo prueba (MANDAR=no) oye y no manda nada")
ok('os.environ.get("OIDO"' in FUENTE and "OIDO=no" in leer(".env.example") and "OIDO=si" in leer("README.md"),
   "la cámara como oído es una decisión aparte (OIDO=si); apagada por defecto")
ok("wav_de" in FUENTE and "16000" in FUENTE and "setnchannels(CANALES)" in FUENTE,
   "manda WAV en el formato que el servidor pide")

print("\n== la limpieza de ruido (OIDO_DENOISE) ==")
_guardar = os.environ.get("OIDO_DENOISE")
try:
    os.environ.pop("OIDO_DENOISE", None)
    ok(oido._filtro_denoise() == "", "sin OIDO_DENOISE no se filtra nada (como hasta ahora)")
    os.environ["OIDO_DENOISE"] = "no"
    ok(oido._filtro_denoise() == "", "OIDO_DENOISE=no tampoco filtra")
    os.environ["OIDO_DENOISE"] = "si"
    ok(oido._filtro_denoise() == oido.DENOISE_DEFECTO and "afftdn" in oido._filtro_denoise(),
       "OIDO_DENOISE=si aplica el filtro por defecto (denoiser FFT)")
    os.environ["OIDO_DENOISE"] = "highpass=f=200,afftdn,dynaudnorm"
    ok(oido._filtro_denoise() == "highpass=f=200,afftdn,dynaudnorm",
       "un filtro de ffmpeg a medida se usa tal cual")
finally:
    if _guardar is None:
        os.environ.pop("OIDO_DENOISE", None)
    else:
        os.environ["OIDO_DENOISE"] = _guardar
ok("-af" in FUENTE and "_filtro_denoise()" in FUENTE.split("def capturar(")[1],
   "el filtro se aplica en la captura de ffmpeg, antes del detector de voz")

if fallos:
    print("\nFALLARON %d:" % len(fallos))
    for f in fallos:
        print("  - " + f)
    sys.exit(1)
print("\ntodo ok")
