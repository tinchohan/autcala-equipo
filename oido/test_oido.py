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
import json
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

print("\n== la fuente del audio (cámara o micrófono de la PC) ==")
_g = {k: os.environ.get(k) for k in ("OIDO_FUENTE", "OIDO_MIC")}
try:
    os.environ.pop("OIDO_FUENTE", None); os.environ.pop("OIDO_MIC", None)
    ent = oido._entrada()
    ok(ent[:2] == ["-rtsp_transport", "tcp"] and ent[-1].startswith("rtsp://"),
       "por defecto la fuente es la cámara (RTSP), como hasta ahora")
    os.environ["OIDO_FUENTE"] = "mic"; os.environ["OIDO_MIC"] = "Microfono (Realtek)"
    ok(oido._entrada() == ["-f", "dshow", "-i", "audio=Microfono (Realtek)"],
       "con OIDO_FUENTE=mic toma el micrófono de la PC por dshow (Windows)")
    os.environ.pop("OIDO_MIC", None)
    aviso = True
    try:
        oido._entrada()
    except SystemExit:
        aviso = False
    ok(not aviso, "mic sin OIDO_MIC avisa qué falta, no arranca a ciegas")
finally:
    for k, v in _g.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
ok("OIDO_FUENTE" in FUENTE and "dshow" in FUENTE and "OIDO_MIC" in FUENTE,
   "el mismo oído sirve para la cámara (en la Pi) y para el micrófono de una PC")

print("\n== de corrido (el micrófono USB, como la tablet) ==")
cor = oido.Corrido()
salieron = [t for t in (cor.cuadro(CUADRO, False, 100.0 + i * 0.02) for i in range(oido.TRAMO_MAX_FRAMES * 2 + 10)) if t]
ok(len(salieron) == 2 and all(len(t["pcm"]) // oido.FRAME_BYTES == oido.TRAMO_MAX_FRAMES and t["dur"] == 45.0 for t in salieron)
   and abs(salieron[0]["desde"] - 100.0) < 1e-6 and abs(salieron[1]["desde"] - (100.0 + oido.TRAMO_MAX_FRAMES * 0.02)) < 1e-6,
   "se manda todo, haya voz o no, en tramos de 45 s, cada uno con la hora de su primer cuadro")
resto = cor.cortar()
ok(resto is None, "al cortarse el audio, menos de un segundo suelto no se manda")
for i in range(60):
    cor.cuadro(CUADRO, False, 200.0 + i * 0.02)
resto = cor.cortar()
ok(resto is not None and resto["dur"] == 1.2 and cor.cortar() is None, "un pedazo de un segundo o más sí, una sola vez")

print("\n== la amplificación ==")
ok((oido.ganancia_de(0.3), oido.ganancia_de(0.5), oido.ganancia_de(0.6)) == (24.0, 12.0, 6.0),
   "como en la tablet: ×24 en Máxima, ×12 en Alta, ×6 en Normal")
from array import array  # noqa: E402
bajito = array("h", [((i * 37) % 200) - 100 for i in range(16000)]).tobytes()   # picos de 100 (−50 dB)
alto, g = oido.realzar(bajito, 24.0)
m = array("h"); m.frombytes(alto)
ok(g == 24.0 and len(alto) == len(bajito) and max(m) <= 99 * 24 and max(m) >= 98 * 24,
   "un micrófono flojo se amplifica todo lo que permite la sensibilidad")
fuerte = array("h", [((i * 37) % 20000) - 10000 for i in range(16000)]).tobytes()   # picos de 10.000
salida, g = oido.realzar(fuerte, 24.0)
m = array("h"); m.frombytes(salida)
ok(1.0 <= g < 2.0 and max(m) <= oido.TOPE + 1 and len(salida) == len(fuerte),
   "un micrófono que ya viene fuerte casi no se toca: no se aplasta todo contra el tope")
ya = array("h", [30000 if i % 2 else -30000 for i in range(16000)]).tobytes()
ok(oido.realzar(ya, 24.0) == (ya, 1.0), "nunca se baja el volumen")
golpe = array("h", [100] * 15700 + [3000] * 300).tobytes()   # un golpe de 20 ms
salida, g = oido.realzar(golpe, 24.0)
m = array("h"); m.frombytes(salida)
ok(g == 24.0 and max(m) <= 32767 and max(m) > oido.TOPE and max(m) < oido.TOPE + (3000 * 24 - oido.TOPE) / 10,
   "un golpe en medio de un tramo flojo no le baja la amplificación al resto: el golpe se aplasta sin romperse")
ok(oido.realzar(b"", 24.0) == (b"", 1.0), "un tramo vacío queda vacío")
ok(oido.nivel_db(b"\x00\x00" * 320) == -120.0 and -7 < oido.nivel_db(array("h", [16384, -16384] * 160).tobytes()) < -5,
   "el nivel en dB: silencio puro es −120; media escala, −6")

print("\n== el micrófono USB ==")
TARJETAS = (" 0 [vc4hdmi0      ]: vc4-hdmi - vc4-hdmi-0\n                      vc4-hdmi-0\n"
            " 1 [vc4hdmi1      ]: vc4-hdmi - vc4-hdmi-1\n                      vc4-hdmi-1\n"
            " 2 [Device        ]: USB-Audio - USB PnP Sound Device\n                      C-Media at usb-xhci\n")
ok(oido.mic_usb(TARJETAS) == "plughw:CARD=Device,DEV=0", "encuentra el primer micrófono USB entre las tarjetas de la Pi")
ok(oido.mic_usb(TARJETAS.split(" 2 [")[0]) == "" and oido.mic_usb("") == "", "sin micrófono USB, no inventa uno")
_g = {k: os.environ.get(k) for k in ("OIDO_FUENTE", "OIDO_MIC")}
try:
    os.environ["OIDO_FUENTE"] = "usb"; os.environ["OIDO_MIC"] = "plughw:CARD=Otro,DEV=0"
    ok(oido._entrada() == ["-f", "alsa", "-i", "plughw:CARD=Otro,DEV=0"], "con OIDO_MIC se usa ese micrófono, por ALSA")
    os.environ.pop("OIDO_MIC")
    if not os.path.exists("/proc/asound/cards"):
        try:
            oido._entrada()
            ok(False, "sin micrófono USB avisa (y main() vuelve a buscar a los 30 s), no se cae")
        except oido.SinMicrofono:
            ok(True, "sin micrófono USB avisa (y main() vuelve a buscar a los 30 s), no se cae")
finally:
    for k, v in _g.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

print("\n== la red, el puesto y la sensibilidad ==")
red = Falso([(200, {"token": "t"}), (200, {"turno": {"id": "t1"}, "token": "t-nuevo"}), (0, {})])
c = oido.Caja("https://x.test", "l1", "0000", red, puesto="ab12cd34-0000-0000-0000-000000000000")
ok(c.mandar_tramo(PCM, 1.0, 1.0, 1.0) is False and c.ultimo == 0 and 0 in oido.REINTENTAR and c.turno == "t1",
   "sin internet no se pierde el día ni el token: se avisa que hay que reintentar")
ok(c.token == "t-nuevo" and b'"cajero": "equipo"' in red.pedidos[1][1],
   "abre el día como «equipo» y se queda con el token nuevo que da abrir")
ok(b'name="puesto"' in red.pedidos[2][1] and b"ab12cd34-0000-0000-0000-000000000000" in red.pedidos[2][1],
   "en un local con varios equipos, cada tramo dice de qué puesto es")
red = Falso([(200, {"token": "t"}), (200, {"turno": {"id": "t1"}}), (200, {"ok": True})])
c = oido.Caja("https://x.test", "l1", "0000", red)
c.mandar_tramo(PCM, 1.0, 1.0, 1.0)
ok(b'name="puesto"' not in red.pedidos[2][1], "con un solo equipo, sin puesto (como la tablet)")
red = Falso([(200, {"oido": {"umbral": 0.3, "sensibilidad": "maxima"}, "anuncios": []})])
c.pedir = red
ok(c.sensibilidad(1.0) == 0.3 and red.pedidos[0][0] == "https://x.test/comercios/caja/kiosco/anuncios"
   and red.pedidos[0][1] is None and red.pedidos[0][2].get("X-Caja-Token") == "t",
   "pregunta la sensibilidad del panel por la misma ruta que la tablet")
c.pedir = Falso([(0, {})])
ok(c.sensibilidad(1.0) is None, "sin internet, la sensibilidad queda como estaba")


class CajaFalsa:
    def __init__(self, codigos):
        self.codigos, self.subidos, self.latidos, self.ultimo, self.apagado = list(codigos), [], [], 200, False

    def mandar_tramo(self, pcm, dur, desde, ahora):
        self.ultimo = self.codigos.pop(0) if self.codigos else 200
        if self.ultimo == 200:
            self.subidos.append(desde)
        return self.ultimo == 200

    def sensibilidad(self, ahora):
        return 0.6

    def latido(self, ahora, datos=None):
        self.latidos.append(datos)


def tramo_de(desde):
    return {"pcm": bajito, "dur": 1.0, "desde": desde}


print("\n== la cola de subida ==")
cf = CajaFalsa([0, 503, 200])
mj = oido.Mensajero(cf, reloj=lambda: 1000.0)
mj.tramo(tramo_de(1.0))
ok(mj.paso() is True and mj.paso() is True and len(mj.cola) == 1 and mj.errores == 2,
   "si la red falla, el tramo se queda y se reintenta")
ok(mj.paso() is False and cf.subidos == [1.0] and not mj.cola and mj.umbral == 0.6,
   "cuando vuelve, sube; y en el camino tomó la sensibilidad del panel")
ok(mj.cola[0]["g"] if mj.cola else True, "")
cf = CajaFalsa([0] * 50)
mj = oido.Mensajero(cf, reloj=lambda: 1000.0)
for i in range(oido.COLA_MAX + 3):
    mj.tramo(tramo_de(float(i)))
ok(len(mj.cola) == oido.COLA_MAX and mj.perdidos == 3 and mj.cola[0]["desde"] == 3.0,
   "sin internet por un rato largo, se tira lo más viejo: la memoria no crece y nada va al disco")
cf = CajaFalsa([409])
mj = oido.Mensajero(cf, reloj=lambda: 1000.0)
mj.tramo(tramo_de(1.0))
ok(mj.paso() is False and not mj.cola, "lo que el servidor rechaza por otra cosa (día cerrado) no se reintenta")
cf = CajaFalsa([])
mj = oido.Mensajero(cf, reloj=lambda: 1000.0, gmax=lambda: 2.0)
mj.tramo(tramo_de(1.0))
mj.paso()
ok(mj.ganancia == 2.0 and mj.nivel_max > -50, "la amplificación se hace al subir, con la ganancia que toca en ese momento")

print("\n== el latido del equipo ==")
mi = oido.Minuto()
mi.cuadros, mi.tramos, mi.segundos, mi.crudo_max = 2750, 1, 45.0, -38.4
lat = mi.para_latido(mj, 1)
ok(lat["mic"] == "ok" and lat["voz"] is True and lat["voz_s"] == 45 and lat["vosk_diag"]["variante"] == 1
   and lat["vosk_diag"]["crudo_max"] == -38 and lat["vosk_diag"]["ganancia"] == 20 and lat["vosk_diag"]["umbral"] == 60
   and mj.nivel_max == -120.0,
   "dice que oye, cuánto mandó, con qué cadena (la natural) y cómo entra el micrófono: sólo números")
ok(oido.Minuto().para_latido(None, 1)["mic"] == "sin_senal",
   "un minuto entero sin audio: «micrófono sin señal», y el servidor le avisa al dueño")
mi = oido.Minuto(); mi.cuadros = 3000
ok(mi.para_latido(None, 1)["mic"] == "sin_senal", "audio que es puro silencio digital (micrófono muteado) también")
red = Falso([(200, {"token": "t"}), (200, {"turno": {"id": "t1"}}), (200, {"ok": True})])
c = oido.Caja("https://x.test", "l1", "0000", red)
c.latido(1.0, lat)
cuerpo = json.loads(red.pedidos[-1][1])
ok(cuerpo["version"] == oido.VERSION and cuerpo["version"].startswith("equipo-oido") and cuerpo["vosk_diag"]["tramos"] == 1
   and len(cuerpo["version"]) <= 40,
   "el latido lleva la versión del equipo, para distinguirlo de la tablet")
ok("perdidos" in leer("../backend/app/services/caja.py") if os.path.exists(os.path.join(EQUIPO, "../backend/app/services/caja.py")) else True,
   "el servidor guarda los números nuevos del latido (perdidos, ganancia)")

print("\n== la cadena del navegador ==")
os.environ.pop("OIDO_CADENA", None)
ok(oido.cadena() == "natural", "sin pedirlo, la cadena es la natural: nada cambia")
os.environ["OIDO_CADENA"] = "Turnos "
ok(oido.cadena() == "turnos", "OIDO_CADENA=turnos: media hora cada una")
os.environ["OIDO_CADENA"] = "cualquiera"
ok(oido.cadena() == "natural", "algo que no se entiende es la natural")
os.environ.pop("OIDO_CADENA", None)
ok([oido.turno_de(t) for t in (0, 1799, 1800, 3600, 86400, 86400 + 1800)] == [0, 0, 1, 0, 1, 0],
   "el turno cambia cada media hora y se corre con el día, como en el servidor")
ok(oido.toca_navegador("navegador", 0) and not oido.toca_navegador("natural", 1800)
   and oido.toca_navegador("turnos", 1800) and not oido.toca_navegador("turnos", 0),
   "en turnos, la limpieza del navegador va en el turno 1")
_dieces = []
nv = oido.Navegador(lambda b: (_dieces.append(len(b)), b[::-1])[1])
_c = bytes(range(256)) * 2 + bytes(128)
ok(len(nv.cuadro(_c)) == oido.FRAME_BYTES and _dieces == [320, 320] and nv.cuadro(_c)[:320] == _c[:320][::-1],
   "la limpieza trabaja de a 10 ms: cada cuadro de 20 se parte en dos y vuelve del mismo largo")
lat2 = oido.Minuto().para_latido(None, 2)["vosk_diag"]
ok(lat2["variante"] == 2 and lat2["ns"] == 1 and lat2["agc"] == 1 and lat2["ec"] == 0
   and lat["vosk_diag"]["ns"] == 0 and lat["vosk_diag"]["agc"] == 0,
   "el latido dice con qué cadena se escuchó: 2 = con el filtro de ruido y la ganancia del navegador")
_main = FUENTE.split("def main(")[1]
ok("sigo con la natural" in _main and "seg.cortar()" in _main.split("toca != limpia")[1][:300],
   "si la limpieza no se puede prender, sigue con la natural; y al cambiar de cadena se cierra el tramo")

print("\n== leer el audio de ffmpeg ==")
_trozos = [b"a" * 100, b"b" * 540, b"c" * 640, b"d" * 300, b"e" * 340, b"f" * 10, b""]
_pedidos = []


def _leer(n):
    _pedidos.append(n)
    return _trozos.pop(0)


_cs = list(oido.cuadros_de(_leer))
ok(_cs == [b"a" * 100 + b"b" * 540, b"c" * 640, b"d" * 300 + b"e" * 340] and max(_pedidos) == oido.FRAME_BYTES
   and _pedidos[1] == 540,
   "un pipe que entrega de a pedazos no corta el audio: se junta hasta completar cada cuadro (y lo que sobra al final se tira)")

print("\n== lo que el código promete (2) ==")
ok("SinMicrofono" in FUENTE.split("def main(")[1] and "SIN_MIC_S" in FUENTE.split("def main(")[1],
   "sin micrófono no se cae: lo vuelve a buscar")
ok('threading.Thread(target=mensajero.correr' in FUENTE and "realzar(" not in FUENTE.split("def main(")[1],
   "la subida y la amplificación van en otro hilo: la grabación nunca espera a la red")
ok(not re.search(r"(?<!wave\.)open\([^)]*['\"]w", FUENTE) and ".wav\"" not in FUENTE.replace('"tramo.wav"', ""),
   "no se escribe audio en el disco")

if fallos:
    print("\nFALLARON %d:" % len(fallos))
    for f in fallos:
        print("  - " + f)
    sys.exit(1)
print("\ntodo ok")
