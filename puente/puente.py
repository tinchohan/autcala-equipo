# -*- coding: utf-8 -*-
"""El puente del equipo del local: de lo que ve Frigate a lo que recibe el
servidor.

Frigate mira las cámaras y avisa por MQTT cada vez que una persona que viene
siguiendo cambia de zona (`frigate/events`). Este programa escucha esos
avisos y lleva la cuenta:

- TRAMOS de 15 minutos: cuántos entraron, cuántos hubo a la vez en el salón y
  en la fila, cuántos llegaron a la caja y cuánto tardaron desde que
  empezaron a pedir.
- EVENTOS: alguien parado en la caja (`en_caja`) y alguien en el local cuando
  está cerrado (`fuera_de_horario`).

Una vez por minuto manda al servidor lo que cambió, con el PIN de la caja
del local. Si no hay conexión, queda guardado en el disco y sale cuando
vuelve. El formato está en el README.md, «Qué manda».

DE ACÁ NO SALE NINGUNA IMAGEN: sólo cuentas, duraciones y horas. La hora es
la del reloj de este equipo, no la que la cámara escribe sobre la imagen.

Las zonas (los nombres tienen que ser los de frigate/config.yml, que sale de
frigate/config.ejemplo.yml):
  puerta  el umbral: por ahí se entra y se sale
  salon   el resto del piso del lado del cliente
  fila    donde el cliente empieza a pedir
  caja    donde paga

Un local con dos mostradores (el de siempre y, por ejemplo, una heladería)
puede tener `fila` y `caja` en dos cámaras, una por mostrador: cada aviso
sale con el número de su cámara. `puerta` y `salon` van en una sola cámara.

Cómo se cuenta:
- ENTRÓ: una persona que apareció primero en `puerta` y después pisó `salon`,
  `fila` o `caja`. La que aparece adentro y termina en la puerta, salió.
- PASÓ: cada persona distinta que pisa `salon`, una vez, venga de donde venga.
  Es la circulación: si la zona incluye la vereda que se ve por el vidrio,
  cuenta también a quien camina frente al local sin entrar.
- ESPERA: lo que pasa entre que pisa `fila` y que llega a `caja`.
- EN LA CAJA: lo que estuvo parada en `caja`, si fueron 8 segundos o más.
- LLEGÓ A LA CAJA (a_caja): cada persona distinta, una vez, cuando suma
  CAJA_MIN_S segundos (30 por defecto, en el .env) dentro de `caja`, en una
  vez o en varias (vuelve por el vuelto). El que la cruza de pasada, o la
  gente de la vereda si la zona alcanza el vidrio, no llega a sumarlos.
- FUERA DE HORARIO: cualquier persona vista 10 segundos o más con el local
  cerrado, esté o no en una zona (también detrás del mostrador).

La lógica (la clase Diario) no depende de MQTT ni de la red: las pruebas la
usan con avisos armados a mano (test_puente.py).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger("puente")

TRAMO_S = 900
PUERTA, SALON, FILA, CAJA = "puerta", "salon", "fila", "caja"
DEL_CLIENTE = {PUERTA, SALON, FILA, CAJA}
ADENTRO = {SALON, FILA, CAJA}
ESPERA_MIN_S = 5            # menos que esto no es una espera: pasó de largo
ESPERA_LARGA_S = 300        # lo que el servidor llama espera larga: más de 5 minutos
ESPERA_MAX_S = 7200
CAJA_MIN_S = 8


def _segundos_env(nombre: str, defecto: int) -> int:
    try:
        return max(1, int(os.environ.get(nombre, "") or defecto))
    except ValueError:
        return defecto


A_CAJA_MIN_S = _segundos_env("CAJA_MIN_S", 30)   # lo que tiene que estar en la caja para contar como cliente
FUERA_MIN_S = 10
OLVIDO_S = 600              # una persona de la que Frigate no dijo más nada en 10 minutos se da por ida
GUARDAR_DIAS = 8            # lo ya mandado se borra del disco a los 8 días (el servidor acepta hasta 7 de atraso)
POR_ENVIO_TRAMOS = 150      # el servidor acepta 200 tramos y 300 eventos por envío
POR_ENVIO_EVENTOS = 250
ZONA = timezone(timedelta(hours=-3))   # la hora del local: Argentina, sin horario de verano


def tramo_de(t: float) -> str:
    """El comienzo del cuarto de hora de un momento (segundos desde 1970), en UTC y como texto."""
    return datetime.fromtimestamp(t - t % TRAMO_S, timezone.utc).isoformat()


def hora_de(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).replace(microsecond=0).isoformat()


def minutos_de(texto: str) -> int:
    """«07:30» → 450. Lo que no se entiende, -1."""
    try:
        h, m = (int(x) for x in texto.strip().split(":"))
    except (AttributeError, ValueError):
        return -1
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else -1


def cerrado(t: float, abre: str, cierra: str) -> bool:
    """Si el local está cerrado en ese momento. Sirve también cuando cierra
    pasada la medianoche (abre 07:00, cierra 00:30). Sin horario, nunca."""
    a, c = minutos_de(abre), minutos_de(cierra)
    if a < 0 or c < 0 or a == c:
        return False
    local = datetime.fromtimestamp(t, ZONA)
    ahora = local.hour * 60 + local.minute
    abierto = a <= ahora < c if a < c else (ahora >= a or ahora < c)
    return not abierto


# ── El disco ────────────────────────────────────────────────────────────────

class Disco:
    """Lo que falta mandar, en un archivo: sobrevive a un corte de luz o de
    internet. Cada renglón tiene un número que sube con cada cambio; al
    mandarlo se anota con qué número salió."""

    def __init__(self, ruta: str = ":memory:"):
        self.db = sqlite3.connect(ruta, check_same_thread=False)
        for tabla in ("tramos", "eventos"):
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {tabla} (clave TEXT PRIMARY KEY, datos TEXT NOT NULL, "
                            "cambio INTEGER NOT NULL DEFAULT 1, enviado INTEGER NOT NULL DEFAULT 0, "
                            "tocado REAL NOT NULL)")
        self.db.commit()

    def leer(self, tabla: str, clave: str) -> dict | None:
        fila = self.db.execute(f"SELECT datos FROM {tabla} WHERE clave = ?", (clave,)).fetchone()
        return json.loads(fila[0]) if fila else None

    def guardar(self, tabla: str, clave: str, datos: dict, ahora: float) -> None:
        self.db.execute(f"INSERT INTO {tabla} (clave, datos, tocado) VALUES (?, ?, ?) "
                        "ON CONFLICT(clave) DO UPDATE SET datos = excluded.datos, cambio = cambio + 1, "
                        "tocado = excluded.tocado", (clave, json.dumps(datos), ahora))
        self.db.commit()

    def pendientes(self, tabla: str, tope: int) -> list:
        """Los más viejos primero: [(clave, datos, cambio)]."""
        filas = self.db.execute(f"SELECT clave, datos, cambio FROM {tabla} WHERE cambio > enviado "
                                "ORDER BY clave LIMIT ?", (tope,)).fetchall()
        return [(c, json.loads(d), n) for c, d, n in filas]

    def marcar(self, tabla: str, enviados: list) -> None:
        self.db.executemany(f"UPDATE {tabla} SET enviado = ? WHERE clave = ? AND enviado < ?",
                            [(n, c, n) for c, _d, n in enviados])
        self.db.commit()

    def limpiar(self, ahora: float) -> None:
        for tabla in ("tramos", "eventos"):
            self.db.execute(f"DELETE FROM {tabla} WHERE cambio <= enviado AND tocado < ?",
                            (ahora - GUARDAR_DIAS * 86400,))
        self.db.commit()


# ── La cuenta ───────────────────────────────────────────────────────────────

TRAMO_VACIO = {"entraron": 0, "en_salon": 0, "en_fila": 0, "esperas": 0, "espera_s": 0, "espera_max_s": 0,
               "esperas_largas": 0, "a_caja": 0, "pasaron": 0, "camaras": 0}


class Diario:
    """Lleva la cuenta con los avisos de Frigate. No sabe de MQTT ni de la red."""

    def __init__(self, disco: Disco, abre: str = "", cierra: str = ""):
        self.disco, self.abre, self.cierra = disco, abre, cierra
        self.personas: dict = {}     # id de Frigate → lo que se sabe de esa persona
        self.camaras: dict = {}      # cámara → si Frigate la está viendo
        # Los avisos llegan por un hilo y la vuelta de cada minuto corre en otro: de a uno por vez.
        self.candado = threading.RLock()

    # -- los tramos --
    def _tramo(self, t: float) -> tuple:
        # Merge con TRAMO_VACIO: un tramo guardado por una versión vieja (sin
        # algún campo, p. ej. a_caja) arranca igual con todos los campos, en 0.
        clave = tramo_de(t)
        return clave, {**TRAMO_VACIO, **(self.disco.leer("tramos", clave) or {})}

    def _sumar(self, t: float, **cuentas) -> None:
        clave, tr = self._tramo(t)
        for k, v in cuentas.items():
            tr[k] += v
        self.disco.guardar("tramos", clave, tr, t)

    def _presentes(self, t: float) -> None:
        """Cuántos hay ahora en el salón y en la fila: queda el máximo del tramo.

        En el salón no se suman las cámaras: dos cámaras que ven el mismo
        piso ven a la misma gente, así que vale la que más ve. En la fila sí
        se suman, porque cada cámara con fila mira un mostrador distinto."""
        por_camara: dict = {}
        for p in self.personas.values():
            if p["zonas"] & DEL_CLIENTE:
                por_camara[p["camara"]] = por_camara.get(p["camara"], 0) + 1
        en_salon = max(por_camara.values(), default=0)
        en_fila = sum(1 for p in self.personas.values() if p["zonas"] & {FILA, CAJA})
        clave, tr = self._tramo(t)
        if en_salon > tr["en_salon"] or en_fila > tr["en_fila"]:
            tr["en_salon"], tr["en_fila"] = max(tr["en_salon"], en_salon), max(tr["en_fila"], en_fila)
            self.disco.guardar("tramos", clave, tr, t)

    # -- los avisos de Frigate --
    def camara(self, nombre: str, estado: str) -> None:
        """`frigate/<cámara>/status/detect`: online, offline o disabled."""
        with self.candado:
            self.camaras[nombre] = estado == "online"

    def aviso(self, mensaje: dict, ahora: float) -> None:
        """Un mensaje de `frigate/events`. Lo que no es una persona, se ignora."""
        with self.candado:
            self._aviso(mensaje, ahora)

    def _aviso(self, mensaje: dict, ahora: float) -> None:
        d = mensaje.get("after") if isinstance(mensaje, dict) else None
        if not isinstance(d, dict) or d.get("label") != "person" or not d.get("id") or d.get("false_positive"):
            return
        t = float(d.get("frame_time") or ahora)
        pid, fin = str(d["id"]), mensaje.get("type") == "end"
        zonas = {z for z in (d.get("current_zones") or []) if isinstance(z, str)}
        p = self.personas.get(pid)
        if p is None:
            p = self.personas[pid] = {"camara": d.get("camera") or "", "inicio": float(d.get("start_time") or t),
                                      "primera": None, "entro": False, "fila_desde": None, "espero": False,
                                      "caja_desde": None, "caja_primera": None, "caja_s": 0.0, "conto_caja": False,
                                      "conto_salon": False, "zonas": set(), "fuera": False, "visto": t}
        nuevas = zonas - p["zonas"]
        if p["primera"] is None and zonas:
            # Si aparece en dos zonas a la vez no se sabe por dónde vino: no cuenta como entrada.
            p["primera"] = next(iter(zonas)) if len(zonas) == 1 else "varias"
        if not p["entro"] and p["primera"] == PUERTA and zonas & ADENTRO:
            p["entro"] = True
            self._sumar(t, entraron=1)
        if SALON in zonas and not p["conto_salon"]:   # la circulación: cada persona distinta que pasa, una vez
            p["conto_salon"] = True
            self._sumar(t, pasaron=1)
        if FILA in nuevas and p["fila_desde"] is None:
            p["fila_desde"] = t
        if CAJA in nuevas:
            if p["fila_desde"] is not None and not p["espero"]:
                espera = int(min(ESPERA_MAX_S, t - p["fila_desde"]))
                if espera >= ESPERA_MIN_S:
                    p["espero"] = True
                    clave, tr = self._tramo(t)
                    tr["esperas"] += 1
                    tr["espera_s"] += espera
                    tr["espera_max_s"] = max(tr["espera_max_s"], espera)
                    tr["esperas_largas"] += 1 if espera > ESPERA_LARGA_S else 0
                    self.disco.guardar("tramos", clave, tr, t)
            p["caja_desde"] = t
            p["caja_primera"] = p["caja_primera"] or t
        if p["caja_desde"] is not None and (CAJA not in zonas or fin):
            p["caja_s"] += max(0.0, t - p["caja_desde"])
            p["caja_desde"] = None
            if p["caja_s"] >= CAJA_MIN_S:
                self.disco.guardar("eventos", f"caja-{pid}", {
                    "clave": f"caja-{pid}"[:80], "tipo": "en_caja", "camara": self._numero(p["camara"]),
                    "desde": hora_de(p["caja_primera"]), "segundos": int(p["caja_s"])}, t)
        self._contar_caja(p, t)
        if cerrado(t, self.abre, self.cierra) and t - p["inicio"] >= FUERA_MIN_S:
            p["fuera"] = True
        if p["fuera"] and (fin or not self.disco.leer("eventos", f"fuera-{pid}")):
            self.disco.guardar("eventos", f"fuera-{pid}", {
                "clave": f"fuera-{pid}"[:80], "tipo": "fuera_de_horario", "camara": self._numero(p["camara"]),
                "desde": hora_de(p["inicio"]), "segundos": int(max(0.0, t - p["inicio"]))}, t)
        p["zonas"], p["visto"] = zonas, t
        if fin:
            self.personas.pop(pid, None)
        self._presentes(t)

    def _contar_caja(self, p: dict, t: float) -> None:
        """Cliente en la caja: la persona cuenta una vez cuando suma A_CAJA_MIN_S
        dentro de `caja` (lo de antes más lo que lleva ahora, si sigue ahí)."""
        if p["conto_caja"]:
            return
        total = p["caja_s"] + (t - p["caja_desde"] if p["caja_desde"] is not None else 0.0)
        if total >= A_CAJA_MIN_S:
            p["conto_caja"] = True
            self._sumar(t, a_caja=1)

    @staticmethod
    def _numero(camara: str) -> int:
        """El número de la cámara, si el nombre termina en uno («camara2» → 2). Si no, 0."""
        digitos = "".join(c for c in camara[-2:] if c.isdigit())
        return int(digitos) if digitos else 0

    # -- una vez por minuto --
    def minuto(self, ahora: float) -> None:
        """Deja el tramo en curso al día (con cuántas cámaras se ven) aunque no
        haya pasado nadie: así el servidor sabe que el equipo sigue mirando.
        Y olvida a quien Frigate dejó de nombrar."""
        with self.candado:
            self._minuto(ahora)

    def _minuto(self, ahora: float) -> None:
        for pid in [k for k, p in self.personas.items() if ahora - p["visto"] > OLVIDO_S]:
            self.personas.pop(pid, None)
        for p in self.personas.values():      # quien sigue quieto pagando no manda avisos: se mira acá
            self._contar_caja(p, ahora)
        clave, tr = self._tramo(ahora)
        tr["camaras"] = sum(1 for viva in self.camaras.values() if viva)
        self.disco.guardar("tramos", clave, tr, ahora)
        self._presentes(ahora)
        self.disco.limpiar(ahora)

    def para_enviar(self) -> tuple:
        with self.candado:
            return (self.disco.pendientes("tramos", POR_ENVIO_TRAMOS),
                    self.disco.pendientes("eventos", POR_ENVIO_EVENTOS))

    def enviado(self, tramos: list, eventos: list) -> None:
        """El servidor guardó esa tanda. Lo que cambió mientras viajaba sigue pendiente."""
        with self.candado:
            self.disco.marcar("tramos", tramos)
            self.disco.marcar("eventos", eventos)

    @staticmethod
    def cuerpo(tramos: list, eventos: list) -> dict:
        """Lo que se manda al servidor, con la forma del README.md."""
        return {"tramos": [{"desde": clave, **datos} for clave, datos, _n in tramos],
                "eventos": [datos for _c, datos, _n in eventos]}


# ── La red ──────────────────────────────────────────────────────────────────

class Servidor:
    """Entra con el PIN de la caja y manda. Un PIN mal puesto traba la caja
    del local después de varios intentos: si el servidor lo rechaza, espera
    un buen rato antes de volver a probar."""

    ESPERA_PIN_S = 900

    def __init__(self, api: str, local_id: str, pin: str, pedir=None):
        self.api, self.local_id, self.pin = api.rstrip("/"), local_id, pin
        self.token, self.no_antes = "", 0.0
        self.pedir = pedir or self._pedir

    @staticmethod
    def _pedir(url: str, cuerpo: dict, cabeceras: dict) -> tuple:
        """(código, respuesta). Un error de red levanta OSError."""
        pedido = urllib.request.Request(url, data=json.dumps(cuerpo).encode("utf-8"), method="POST", headers={
            "Content-Type": "application/json", "User-Agent": "autcala-equipo/1", **cabeceras})
        try:
            with urllib.request.urlopen(pedido, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, {}

    def entrar(self, ahora: float) -> bool:
        if ahora < self.no_antes:
            return False
        codigo, r = self.pedir(f"{self.api}/comercios/caja/{self.local_id}/entrar", {"pin": self.pin}, {})
        if codigo == 200 and r.get("token"):
            self.token = r["token"]
            return True
        if codigo in (401, 429):
            self.no_antes = ahora + self.ESPERA_PIN_S
            log.error("el servidor no aceptó el PIN (%s): vuelvo a probar en 15 minutos", codigo)
        else:
            log.warning("no pude entrar (%s)", codigo)
        return False

    def mandar(self, cuerpo: dict, ahora: float) -> bool:
        """True si el servidor lo guardó."""
        if not self.token and not self.entrar(ahora):
            return False
        url = f"{self.api}/comercios/caja/ojo"
        codigo, r = self.pedir(url, cuerpo, {"X-Caja-Token": self.token})
        if codigo == 401:   # el token venció (dura 30 horas) o cambió el PIN: se entra de nuevo, una vez
            self.token = ""
            if not self.entrar(ahora):
                return False
            codigo, r = self.pedir(url, cuerpo, {"X-Caja-Token": self.token})
        if codigo == 200:
            if r.get("descartados"):
                log.warning("el servidor descartó %s", r["descartados"])
            return True
        log.warning("el servidor contestó %s: queda guardado y sale después", codigo)
        return False


class SoloMirar:
    """Para probar en casa (MANDAR=no en el .env): no manda nada a ningún
    lado. Escribe en el registro lo que hubiera mandado y lo da por enviado."""

    def mandar(self, cuerpo: dict, ahora: float) -> bool:
        for tr in cuerpo["tramos"]:
            log.info("tramo %s: %s", tr["desde"], ", ".join(f"{k} {v}" for k, v in tr.items() if k != "desde" and v))
        for ev in cuerpo["eventos"]:
            log.info("evento %s en la cámara %s: desde %s, %s s", ev["tipo"], ev["camara"], ev["desde"], ev["segundos"])
        return True


def vuelta(diario: Diario, servidor: Servidor, ahora: float) -> bool:
    """Lo de cada minuto: poner el tramo al día y mandar lo que falta, de a
    tandas. Si algo falla queda en el disco. True si no quedó nada."""
    diario.minuto(ahora)
    for _ in range(20):
        tramos, eventos = diario.para_enviar()
        if not tramos and not eventos:
            return True
        try:
            if not servidor.mandar(Diario.cuerpo(tramos, eventos), ahora):
                return False
        except (OSError, ValueError) as e:
            log.warning("sin conexión (%s): queda guardado", type(e).__name__)
            return False
        diario.enviado(tramos, eventos)
    return False


def main() -> None:
    import paho.mqtt.client as mqtt   # sólo hace falta acá: las pruebas no lo usan

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    solo_mirar = os.environ.get("MANDAR", "si").strip().lower() in ("no", "0", "false")
    falta = [k for k in ("LOCAL_ID", "PIN") if not os.environ.get(k)]
    if falta and not solo_mirar:
        raise SystemExit("Falta en el archivo .env: " + ", ".join(falta) + " (o poner MANDAR=no para probar sin mandar)")
    diario = Diario(Disco(os.environ.get("DISCO", "/datos/puente.db")),
                    os.environ.get("ABRE", ""), os.environ.get("CIERRA", ""))
    servidor = SoloMirar() if solo_mirar else Servidor(os.environ.get("API", "https://api-bis.autcala.com/api/v1"),
                                                       os.environ["LOCAL_ID"], os.environ["PIN"])
    prefijo = os.environ.get("MQTT_PREFIJO", "frigate")

    def al_conectar(cliente, _datos, _banderas, codigo, _props=None):
        log.info("conectado a MQTT (%s)", codigo)
        cliente.subscribe([(f"{prefijo}/events", 0), (f"{prefijo}/+/status/detect", 0)])

    def al_llegar(_cliente, _datos, m):
        try:
            partes = m.topic.split("/")
            if partes[-1] == "events":
                diario.aviso(json.loads(m.payload.decode("utf-8")), time.time())
            elif len(partes) == 4 and partes[2] == "status":
                diario.camara(partes[1], m.payload.decode("utf-8").strip())
        except Exception as e:  # noqa: BLE001  (un aviso raro no tiene que tirar el puente)
            log.warning("no entendí un aviso de %s (%s)", m.topic, type(e).__name__)

    cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="puente")
    cliente.on_connect, cliente.on_message = al_conectar, al_llegar
    cliente.connect_async(os.environ.get("MQTT_HOST", "mosquitto"), int(os.environ.get("MQTT_PUERTO", "1883")))
    cliente.loop_start()
    log.info("puente %s, con el horario %s-%s", "de prueba: NO manda nada" if solo_mirar else "del local " + os.environ["LOCAL_ID"],
             diario.abre or "?", diario.cierra or "?")
    while True:
        try:
            vuelta(diario, servidor, time.time())
        except Exception as e:  # noqa: BLE001
            log.error("la vuelta falló (%s)", type(e).__name__)
        time.sleep(60 - time.time() % 60)


if __name__ == "__main__":
    main()
