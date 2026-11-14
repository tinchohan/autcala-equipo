# -*- coding: utf-8 -*-
"""El puente del equipo del local: de los avisos de Frigate a lo que recibe
el servidor.

Cómo se cuenta una entrada, una espera, alguien en la caja y alguien fuera de
horario; que lo guardado sobrevive a un corte y no se manda dos veces de más;
que un PIN rechazado no se vuelve a probar enseguida; y que lo que sale tiene
la forma que el servidor acepta (se lo pasa por el limpiador de verdad).

Corre sin MQTT, sin red y sin Frigate: los avisos se arman a mano.
    python -X utf8 test_puente.py
"""
import io
import os
import re
import sys
from datetime import datetime, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
EQUIPO = os.path.dirname(AQUI)
sys.path.insert(0, AQUI)
# El limpiador del servidor está en el repositorio grande. En el repositorio
# del equipo solo no está: esa prueba se saltea y se avisa.
sys.path.insert(0, os.path.join(os.path.dirname(EQUIPO), "backend"))

import puente  # noqa: E402

try:
    from app.services.facturacion import ojo  # noqa: E402
except ImportError:
    ojo = None

fallos = []


def ok(cond, etiqueta):
    print(("  OK     " if cond else "  FALLA  ") + etiqueta)
    if not cond:
        fallos.append(etiqueta)


def leer(rel):
    return io.open(os.path.join(EQUIPO, rel), encoding="utf-8").read()


def a_las(hora, minuto=0, segundo=0, dia=13):
    """Una hora de reloj del local (viernes 13 de noviembre de 2026), en segundos desde 1970."""
    return datetime(2026, 11, dia, hora, minuto, segundo, tzinfo=puente.ZONA).timestamp()


def aviso(pid, t, zonas, tipo="update", camara="camara1", inicio=None, label="person", **mas):
    return {"type": tipo, "before": {}, "after": {"id": pid, "camera": camara, "label": label, "frame_time": t,
                                                  "start_time": inicio if inicio is not None else t,
                                                  "current_zones": list(zonas), "false_positive": False, **mas}}


def nuevo(abre="07:00", cierra="00:30"):
    return puente.Diario(puente.Disco(), abre, cierra)


def tramo(d, t):
    return d.disco.leer("tramos", puente.tramo_de(t)) or {}


print("\n== las horas ==")
T0 = a_las(13, 7, 20)
ok(puente.tramo_de(T0) == "2026-11-13T16:00:00+00:00" and puente.tramo_de(a_las(13, 15)) == "2026-11-13T16:15:00+00:00"
   and puente.hora_de(T0 + 0.6) == "2026-11-13T16:07:20+00:00",
   "cada momento cae en su cuarto de hora, en UTC")
ok(puente.cerrado(a_las(3, 12), "07:00", "00:30") and not puente.cerrado(a_las(0, 10), "07:00", "00:30")
   and not puente.cerrado(a_las(13), "07:00", "00:30") and puente.cerrado(a_las(6, 59), "07:00", "00:30")
   and puente.cerrado(a_las(23, 30), "10:00", "23:00") and not puente.cerrado(a_las(22, 59), "10:00", "23:00"),
   "el horario se entiende aunque el local cierre pasada la medianoche")
ok(not puente.cerrado(a_las(3), "", "") and not puente.cerrado(a_las(3), "siempre", "07:00")
   and not puente.cerrado(a_las(3), "07:00", "07:00") and puente.minutos_de("7:30") == 450 and puente.minutos_de("25:00") == -1,
   "sin horario (o con uno que no se entiende) nunca está cerrado: no se avisa nada")

print("\n== entrar ==")
d = nuevo()
d.aviso(aviso("a", T0, ["puerta"], "new"), T0)
ok(tramo(d, T0).get("entraron", 0) == 0, "pisar la puerta todavía no es entrar")
d.aviso(aviso("a", T0 + 3, ["salon"]), T0 + 3)
d.aviso(aviso("a", T0 + 9, ["caja"]), T0 + 9)
ok(tramo(d, T0)["entraron"] == 1, "apareció en la puerta y pisó el salón: entró, y se cuenta una sola vez")
d.aviso(aviso("b", T0 + 10, ["salon"], "new"), T0 + 10)
d.aviso(aviso("b", T0 + 14, ["puerta"]), T0 + 14)
d.aviso(aviso("b", T0 + 16, [], "end"), T0 + 16)
ok(tramo(d, T0)["entraron"] == 1, "el que aparece adentro y termina en la puerta, salió: no cuenta")
d.aviso(aviso("c", T0 + 20, ["puerta", "salon"], "new"), T0 + 20)
d.aviso(aviso("c", T0 + 22, ["salon"]), T0 + 22)
d.aviso(aviso("e", T0 + 23, [], "new"), T0 + 23)                      # detrás del mostrador: sin zona
d.aviso(aviso("f", T0 + 24, ["puerta"], "new", label="car"), T0 + 24)
d.aviso(aviso("f", T0 + 25, ["salon"], label="car"), T0 + 25)
d.aviso(aviso("g", T0 + 24, ["puerta"], "new", false_positive=True), T0 + 24)
d.aviso({"type": "new"}, T0)
d.aviso(None, T0)
ok(tramo(d, T0)["entraron"] == 1 and set(d.personas) == {"a", "c", "e"},
   "si aparece en dos zonas a la vez no se sabe por dónde vino; lo que no es una persona, o Frigate duda, se ignora")
ok(tramo(d, T0)["en_salon"] == 2 and tramo(d, T0)["en_fila"] == 1,
   "cuántos hubo a la vez en el salón y en la fila (los del otro lado del mostrador no cuentan)")

print("\n== la espera y la caja ==")
d = nuevo()
d.aviso(aviso("a", T0, ["fila"], "new"), T0)
d.aviso(aviso("a", T0 + 200, ["caja"]), T0 + 200)
d.aviso(aviso("b", T0 + 10, ["fila"], "new"), T0 + 10)
d.aviso(aviso("b", T0 + 410, ["caja"]), T0 + 410)
d.aviso(aviso("c", T0 + 20, ["fila"], "new"), T0 + 20)
d.aviso(aviso("c", T0 + 23, ["caja"]), T0 + 23)                       # pasó de largo: no es una espera
d.aviso(aviso("e", T0 + 30, ["caja"], "new"), T0 + 30)                # vino derecho a pagar
d.aviso(aviso("f", T0 + 40, ["fila"], "new"), T0 + 40)
d.aviso(aviso("f", T0 + 300, [], "end"), T0 + 300)                    # se fue sin llegar a la caja
tr = tramo(d, T0)
ok((tr["esperas"], tr["espera_s"], tr["espera_max_s"], tr["esperas_largas"]) == (2, 600, 400, 1),
   "la espera va de pisar la fila a llegar a la caja; se anota la más larga y las de más de 5 minutos")
d.aviso(aviso("a", T0 + 230, ["salon"]), T0 + 230)                    # 30 s en la caja
d.aviso(aviso("a", T0 + 240, ["caja"]), T0 + 240)                     # vuelve por el vuelto
d.aviso(aviso("a", T0 + 255, ["puerta"], "end"), T0 + 255)
d.aviso(aviso("c", T0 + 27, ["salon"]), T0 + 27)                      # 4 s en la caja: no es nada
d.aviso(aviso("e", T0 + 90, ["caja"], "end"), T0 + 90)                # Frigate la pierde estando en la caja
ok(d.disco.leer("eventos", "caja-a") == {"clave": "caja-a", "tipo": "en_caja", "camara": 1,
                                         "desde": "2026-11-13T16:10:40+00:00", "segundos": 45}
   and d.disco.leer("eventos", "caja-c") is None and d.disco.leer("eventos", "caja-e")["segundos"] == 60,
   "alguien en la caja: desde cuándo y cuánto estuvo en total, si fueron 8 segundos o más")
ok(tramo(d, T0)["esperas"] == 2, "volver a la caja no cuenta otra espera")
ok(puente.Diario._numero("camara2") == 2 and puente.Diario._numero("cam12") == 12 and puente.Diario._numero("mostrador") == 0,
   "el número de la cámara sale de su nombre")

print("\n== fuera de horario ==")
NOCHE = a_las(3, 12)
d = nuevo()
d.aviso(aviso("n", NOCHE, [], "new"), NOCHE)
ok(d.disco.leer("eventos", "fuera-n") is None, "alguien que se ve un instante con el local cerrado todavía no es un aviso")
d.aviso(aviso("n", NOCHE + 12, []), NOCHE + 12)
ok(d.disco.leer("eventos", "fuera-n") == {"clave": "fuera-n", "tipo": "fuera_de_horario", "camara": 1,
                                          "desde": "2026-11-13T06:12:00+00:00", "segundos": 12},
   "a los 10 segundos sí, esté o no en una zona (también detrás del mostrador)")
d.aviso(aviso("n", NOCHE + 40, []), NOCHE + 40)
d.aviso(aviso("n", NOCHE + 95, [], "end"), NOCHE + 95)
ok(d.disco.leer("eventos", "fuera-n")["segundos"] == 95, "y cuando se va, queda cuánto estuvo")
d = nuevo()
d.aviso(aviso("m", T0, [], "new"), T0)
d.aviso(aviso("m", T0 + 60, [], "end"), T0 + 60)
sin_horario = nuevo("", "")
sin_horario.aviso(aviso("m", NOCHE, [], "new"), NOCHE)
sin_horario.aviso(aviso("m", NOCHE + 60, [], "end"), NOCHE + 60)
ok(not d.disco.pendientes("eventos", 10) and not sin_horario.disco.pendientes("eventos", 10),
   "con el local abierto, o sin horario cargado, no se avisa")

print("\n== cada minuto ==")
d = nuevo()
d.camara("camara1", "online")
d.camara("camara2", "online")
d.camara("camara3", "offline")
d.aviso(aviso("a", T0, ["salon"], "new"), T0)
d.minuto(T0 + 30)
ok(tramo(d, T0)["camaras"] == 2 and "a" in d.personas, "el tramo en curso dice cuántas cámaras se están viendo")
d.minuto(T0 + 30 + puente.OLVIDO_S + 1)
ok("a" not in d.personas, "a quien Frigate dejó de nombrar hace 10 minutos se lo da por ido")
vacio = nuevo()
vacio.minuto(T0)
ok(tramo(vacio, T0) == dict(puente.TRAMO_VACIO) and len(vacio.para_enviar()[0]) == 1,
   "aunque no pase nadie hay tramo: así el servidor sabe que el equipo sigue mirando")

print("\n== el disco ==")
disco = puente.Disco()
disco.guardar("tramos", "t1", {"entraron": 1}, T0)
primera = disco.pendientes("tramos", 10)
disco.guardar("tramos", "t1", {"entraron": 2}, T0 + 5)            # cambió mientras viajaba
disco.marcar("tramos", primera)
ok(disco.pendientes("tramos", 10) == [("t1", {"entraron": 2}, 2)], "lo que cambió mientras viajaba sigue pendiente")
disco.marcar("tramos", disco.pendientes("tramos", 10))
ok(disco.pendientes("tramos", 10) == [] and disco.leer("tramos", "t1") == {"entraron": 2}, "lo mandado no se manda de nuevo")
disco.guardar("eventos", "viejo", {"x": 1}, T0)
disco.limpiar(T0 + 9 * 86400)
ok(disco.leer("tramos", "t1") is None and disco.leer("eventos", "viejo") == {"x": 1},
   "a los 8 días se borra lo ya mandado; lo que no salió se queda")
ruta = os.path.join(AQUI, "_prueba.db")
if os.path.exists(ruta):
    os.remove(ruta)
uno = puente.Disco(ruta)
uno.guardar("tramos", "t9", {"entraron": 4}, T0)
uno.db.close()
dos = puente.Disco(ruta)
ok(dos.pendientes("tramos", 10) == [("t9", {"entraron": 4}, 1)], "lo guardado sobrevive a un corte de luz")
dos.db.close()
os.remove(ruta)

print("\n== mandar ==")


class Falso:
    """Un servidor de mentira: anota lo que le piden y contesta lo que se le diga."""

    def __init__(self, respuestas):
        self.respuestas, self.pedidos = list(respuestas), []

    def __call__(self, url, cuerpo, cabeceras):
        self.pedidos.append((url, cuerpo, cabeceras))
        r = self.respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


d = nuevo()
d.aviso(aviso("a", T0, ["puerta"], "new"), T0)
d.aviso(aviso("a", T0 + 4, ["caja"]), T0 + 4)
d.aviso(aviso("a", T0 + 40, ["puerta"], "end"), T0 + 40)
red = Falso([(200, {"token": "tk1"}), (200, {"tramos": 1, "eventos": 1, "descartados": 0})])
s = puente.Servidor("https://api.ejemplo.test/api/v1/", "l1", "0000", red)
ok(puente.vuelta(d, s, T0 + 60) is True and d.para_enviar() == ([], []),
   "entra con el PIN, manda y no queda nada pendiente")
(u1, c1, h1), (u2, c2, h2) = red.pedidos
ok(u1 == "https://api.ejemplo.test/api/v1/comercios/caja/l1/entrar" and c1 == {"pin": "0000"} and h1 == {}
   and u2 == "https://api.ejemplo.test/api/v1/comercios/caja/ojo" and h2 == {"X-Caja-Token": "tk1"},
   "por las dos rutas de la caja: el PIN y, con el token, lo que contó")
ok(c2 == {"tramos": [{"desde": "2026-11-13T16:00:00+00:00", "entraron": 1, "en_salon": 1, "en_fila": 1, "esperas": 0,
                      "espera_s": 0, "espera_max_s": 0, "esperas_largas": 0, "camaras": 0}],
          "eventos": [{"clave": "caja-a", "tipo": "en_caja", "camara": 1, "desde": "2026-11-13T16:07:24+00:00",
                       "segundos": 36}]},
   "lo que viaja: el tramo con sus cuentas y el evento con su hora. Nada más")
AHORA_S = datetime.fromtimestamp(T0 + 60, timezone.utc)
if ojo is None:
    print("  --     sin el servidor a mano: no se prueba que acepte lo que sale (se prueba en el repositorio grande)")
else:
    ok(all(ojo.limpiar_tramo(x, AHORA_S) == x for x in c2["tramos"])
       and all(ojo.limpiar_evento(x, AHORA_S) == x for x in c2["eventos"])
       and set(puente.TRAMO_VACIO) == set(ojo.CUENTAS) and puente.TRAMO_S == ojo.TRAMO_S
       and puente.ESPERA_LARGA_S == ojo.ESPERA_LARGA_S and puente.POR_ENVIO_TRAMOS <= ojo.MAX_TRAMOS
       and puente.POR_ENVIO_EVENTOS <= ojo.MAX_EVENTOS and puente.GUARDAR_DIAS * 86400 > ojo.ATRASO_MAX.total_seconds(),
       "el servidor de verdad lo acepta tal cual: los mismos campos, los mismos tipos y los mismos topes")

red = Falso([(401, {}), (200, {"token": "tk2"}), (200, {"descartados": 0})])
s.pedir = red
ok(puente.vuelta(d, s, T0 + 120) is True and [p[2] for p in red.pedidos] == [{"X-Caja-Token": "tk1"}, {},
                                                                              {"X-Caja-Token": "tk2"}],
   "si el token venció, entra de nuevo con el PIN una vez y lo manda")
red = Falso([OSError("sin red")])
s.pedir = red
ok(puente.vuelta(d, s, T0 + 180) is False and len(d.para_enviar()[0]) == 1,
   "sin conexión no se pierde nada: queda en el disco")
red = Falso([(503, {})])
s.pedir = red
ok(puente.vuelta(d, s, T0 + 240) is False and len(d.para_enviar()[0]) == 1, "si el servidor no puede, también")
red = Falso([(200, {"descartados": 0})])
s.pedir = red
ok(puente.vuelta(d, s, T0 + 300) is True and len(red.pedidos[0][1]["tramos"]) == 1 and d.para_enviar() == ([], []),
   "y sale todo cuando vuelve")

mal = puente.Servidor("https://api.ejemplo.test/api/v1", "l1", "9999", Falso([(401, {})]))
ok(puente.vuelta(d, mal, T0 + 360) is False and puente.vuelta(d, mal, T0 + 420) is False
   and puente.vuelta(d, mal, T0 + 360 + mal.ESPERA_PIN_S - 1) is False and len(mal.pedir.pedidos) == 1,
   "un PIN rechazado no se vuelve a probar por 15 minutos: no hay que trabarle la caja al local")
mal.pedir = Falso([(200, {"token": "tk3"}), (200, {})])
ok(puente.vuelta(d, mal, T0 + 360 + mal.ESPERA_PIN_S + 1) is True, "pasado ese rato, prueba de nuevo")

prueba = nuevo()
prueba.aviso(aviso("a", T0, ["puerta"], "new"), T0)
prueba.aviso(aviso("a", T0 + 4, ["salon"]), T0 + 4)
ok(puente.vuelta(prueba, puente.SoloMirar(), T0 + 60) is True and prueba.para_enviar() == ([], [])
   and 'solo_mirar = os.environ.get("MANDAR", "si").strip().lower() in ("no", "0", "false")' in leer("puente/puente.py"),
   "para probar en casa (MANDAR=no) cuenta igual y no manda nada a ningún lado")

muchos = nuevo()
for n in range(puente.POR_ENVIO_TRAMOS + 30):
    muchos.disco.guardar("tramos", puente.tramo_de(T0 - n * 900), dict(puente.TRAMO_VACIO), T0)
red = Falso([(200, {"token": "t"})] + [(200, {})] * 3)
ok(puente.vuelta(muchos, puente.Servidor("https://x.test", "l1", "0000", red), T0) is True
   and [len(p[1]["tramos"]) for p in red.pedidos[1:]] == [puente.POR_ENVIO_TRAMOS, 30]
   and red.pedidos[1][1]["tramos"][0]["desde"] < red.pedidos[2][1]["tramos"][0]["desde"],
   "lo atrasado sale de a tandas, lo más viejo primero")

print("\n== lo que el código promete y no se ve corriendo ==")
FUENTE = leer("puente/puente.py")
COMPOSE = leer("docker-compose.yml")
FRIGATE = leer("frigate/config.ejemplo.yml")
IGNORAR = leer(".gitignore")
INSTALAR = io.open(os.path.join(EQUIPO, "instalar.sh"), encoding="utf-8", newline="").read()
ok(not re.search(r"snapshot|thumbnail|\.jpg|\.png|base64|imagen =|foto", FUENTE.split('"""', 2)[2], re.I),
   "el puente no toca ninguna imagen: sólo cuentas, duraciones y horas")
ok(not [linea for linea in FUENTE.splitlines() if "log." in linea and re.search(r"self\.pin|\"PIN\"\]|token\b(?!\.)", linea)]
   and "requests" not in FUENTE,
   "ni el PIN ni el token van a un log")
ok(all(f"{z}:" in FRIGATE for z in (puente.PUERTA, puente.SALON, puente.FILA, puente.CAJA))
   and "enabled: false" in FRIGATE.split("record:")[1][:80] and "enabled: false" in FRIGATE.split("snapshots:")[1][:80],
   "las zonas de Frigate se llaman como las espera el puente, y Frigate no graba ni saca fotos")
ok({".env", "frigate/config.yml"} <= set(IGNORAR.split()) and "{FRIGATE_RTSP_PASSWORD}" in INSTALAR
   and "PIN=" not in COMPOSE and not re.search(r"rtsp://[^{\s]+:[^{\s]+@", FRIGATE + COMPOSE + INSTALAR)
   and leer(".env.example").count("PIN=\n") == 1 and "FRIGATE_RTSP_PASSWORD=\n" in leer(".env.example"),
   "las claves viven en el .env del equipo, que no entra al repositorio, igual que su configuración de Frigate")
ok("read -r -s -p \"PIN de la caja" in INSTALAR and "read -r -s -p \"Clave del grabador" in INSTALAR
   and "chmod 600 .env" in INSTALAR and 'cp frigate/config.yml "frigate/config.yml.bak-' in INSTALAR
   and "/media/pruebas/camara1.mp4" in FRIGATE and "rtsp://" not in FRIGATE,
   "al configurar, el PIN y la clave no se ven al escribirlos, el .env queda sólo para su usuario y se guarda una copia antes de cambiar Frigate")
ok("1883" not in "".join(l for l in COMPOSE.splitlines() if l.strip().startswith('- "')) and "5000:5000" not in COMPOSE,
   "ni el MQTT ni la puerta sin clave de Frigate quedan abiertos a la red del local")

ok("\r" not in INSTALAR and INSTALAR.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in INSTALAR
   and "CONTROLADOR=no" in INSTALAR and INSTALAR.index('if [ "$CONTROLADOR" = si ]') < INSTALAR.index("hailo_frigate.sh")
   and "rm -rf" not in INSTALAR and "if [ ! -f .env ]; then" in INSTALAR
   and "if [ ! -f frigate/config.yml ]; then" in INSTALAR,
   "el instalador no cambia el controlador del Hailo si no se lo piden, no borra nada y no pisa el .env ni las zonas")
PUBLICO = "".join(leer(x) for x in ("README.md", "puente/puente.py", "frigate/config.ejemplo.yml", ".env.example",
                                    "docker-compose.yml")) + INSTALAR
ok(not re.search(r"docs/ojo|\.\./docs|juramento|subway|\bH4\b|mart[ií]n|@gmail", PUBLICO, re.I),
   "lo que queda a la vista de todos no nombra a ningún local ni a ninguna persona, ni apunta a papeles internos")

if fallos:
    print("\nFALLARON %d:" % len(fallos))
    for f in fallos:
        print("  - " + f)
    sys.exit(1)
print("\ntodo ok")
