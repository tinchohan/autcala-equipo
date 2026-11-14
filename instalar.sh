#!/usr/bin/env bash
# El instalador del equipo del local. Se corre en la Raspberry Pi, parado en
# esta carpeta:
#
#     ./instalar.sh                 instala Docker si falta, prepara todo y lo levanta
#     ./instalar.sh --configurar    pregunta el link de la caja, el PIN, el horario y el grabador
#     ./instalar.sh --controlador   instala el controlador del Hailo que pide Frigate
#     ./instalar.sh --estado        sólo dice cómo está cada cosa
#
# Se puede correr todas las veces que haga falta: lo que ya está, no lo toca.
# No borra nada. El .env y frigate/config.yml son de cada equipo: sólo los
# cambia --configurar, y antes guarda una copia.
#
# OJO con --controlador: reemplaza el controlador del Hailo que instaló
# `hailo-all`. Los ejemplos de ~/hailo-apps pueden dejar de andar. Por eso no
# se hace solo: hay que pedirlo.
set -euo pipefail

cd "$(dirname "$0")"
DOCS_HAILO="https://docs.frigate.video/frigate/installation"
SCRIPT_HAILO="https://raw.githubusercontent.com/blakeblackshear/frigate/dev/docker/hailo8l/user_installation.sh"

dice()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
bien()  { printf '  ok     %s\n' "$*"; }
falta() { printf '  FALTA  %s\n' "$*"; }
corta() { printf '\n%s\n' "$*" >&2; exit 1; }

CONTROLADOR=no
CONFIGURAR=no
SOLO_ESTADO=no
for arg in "$@"; do
  case "$arg" in
    --controlador) CONTROLADOR=si ;;
    --configurar) CONFIGURAR=si ;;
    --estado) SOLO_ESTADO=si ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) corta "No conozco la opción $arg. Probá ./instalar.sh --help" ;;
  esac
done

SISTEMA="$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-desconocido}")"

# ── El .env, de a una línea ─────────────────────────────────────────────────
actual() {  # actual CLAVE: lo que dice hoy el .env
  { grep -m1 "^$1=" .env 2>/dev/null || true; } | cut -d= -f2-
}

poner() {  # poner CLAVE VALOR: cambia (o agrega) esa línea del .env y deja las demás como están
  local clave="$1" valor="$2" linea hecho=no tmp
  tmp="$(mktemp)"
  while IFS= read -r linea || [ -n "$linea" ]; do
    if [[ "$linea" == "$clave="* ]]; then
      printf '%s=%s\n' "$clave" "$valor"
      hecho=si
    else
      printf '%s\n' "$linea"
    fi
  done < .env > "$tmp"
  [ "$hecho" = si ] || printf '%s=%s\n' "$clave" "$valor" >> "$tmp"
  cat "$tmp" > .env
  rm -f "$tmp"
}

codificar() {  # para que una clave con @ : / # no rompa la dirección de la cámara
  local LC_ALL=C s="$1" i c o=""
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) o+="$c" ;;
      *) printf -v c '%%%02X' "'$c"; o+="$c" ;;
    esac
  done
  printf '%s' "$o"
}

hora_bien() { [[ "$1" =~ ^([01]?[0-9]|2[0-3]):[0-5][0-9]$ ]]; }

estado() {
  dice "Cómo está el equipo"
  if [ "$(uname -m)" = "aarch64" ]; then bien "sistema de 64 bits ($SISTEMA)"; else falta "esto no es una Raspberry Pi de 64 bits ($(uname -m))"; fi
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    bien "Docker $(docker --version | awk '{print $3}' | tr -d ,)"
  else
    falta "Docker (lo instala este programa)"
  fi
  if [ -e /dev/hailo0 ]; then
    bien "el Hailo está (/dev/hailo0), controlador $(cat /sys/module/hailo_pci/version 2>/dev/null || echo '?')"
  else
    falta "el Hailo no aparece (/dev/hailo0): revisar la cinta del AI HAT+ o correr ./instalar.sh --controlador"
  fi
  if [ -f .env ]; then
    if [ -n "$(actual LOCAL_ID)" ] && [ -n "$(actual PIN)" ]; then
      bien "configurado para el local $(actual LOCAL_ID) (MANDAR=$(actual MANDAR))"
    else
      bien "en modo de prueba, sin local (./instalar.sh --configurar para ponerle uno)"
    fi
  else
    falta "el archivo .env (lo crea este programa)"
  fi
  if [ -f frigate/config.yml ] && grep -q '/media/pruebas/' frigate/config.yml; then
    local hay=0 n
    for n in 1 2 3 4; do [ -f "clips/camara$n.mp4" ] && hay=$((hay + 1)); done
    if [ "$hay" -gt 0 ]; then bien "Frigate lee clips de prueba ($hay de 4 en clips/)"; else falta "los clips de prueba (clips/camara1.mp4 … camara4.mp4)"; fi
  elif [ -f frigate/config.yml ]; then
    bien "Frigate lee las cámaras del grabador $(actual FRIGATE_NVR_IP)"
  fi
  if command -v docker >/dev/null 2>&1 && [ -n "$(docker compose ps -q 2>/dev/null)" ]; then
    docker compose ps --format '  {{.Service}}: {{.Status}}'
  else
    falta "todavía no está levantado"
  fi
}

configurar() {
  dice "Configurar este equipo para un local"
  echo "Hacen falta el link de la caja del local (está en el panel de Autcala,"
  echo "«Link de la caja») y su PIN. Enter deja lo que ya estaba [entre corchetes]."
  local enlace id pin abre cierra ip puerto usuario clave r

  read -r -p "Link de la caja [$(actual LOCAL_ID)]: " enlace
  if [ -n "$enlace" ]; then
    id="$(printf '%s' "$enlace" | grep -oiE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | tail -n 1 || true)"
    [ -n "$id" ] || corta "No encontré el local en ese link. Tiene que ser como https://dashboard.autcala.com/comercios/caja/xxxxxxxx-xxxx-…"
    poner LOCAL_ID "$id"
  fi
  [ -n "$(actual LOCAL_ID)" ] || corta "Sin el link de la caja no puedo seguir."

  read -r -s -p "PIN de la caja (no se ve al escribirlo)$([ -n "$(actual PIN)" ] && echo ' [el que estaba]'): " pin
  echo
  if [ -n "$pin" ]; then
    [[ "$pin" =~ ^[0-9]{4,8}$ ]] || corta "El PIN son de 4 a 8 números."
    poner PIN "$pin"
  fi
  [ -n "$(actual PIN)" ] || corta "Sin el PIN no puedo seguir."

  echo
  echo "El horario del local, para avisar si se ve a alguien con el local cerrado."
  read -r -p "Abre a las (HH:MM) [$(actual ABRE)]: " abre
  read -r -p "Cierra a las (HH:MM, puede ser pasada la medianoche) [$(actual CIERRA)]: " cierra
  if [ -n "$abre" ]; then hora_bien "$abre" || corta "«$abre» no es una hora. Va como 07:00."; poner ABRE "$abre"; fi
  if [ -n "$cierra" ]; then hora_bien "$cierra" || corta "«$cierra» no es una hora. Va como 23:30."; poner CIERRA "$cierra"; fi

  echo
  echo "El grabador de las cámaras. Enter en la IP lo deja para después (Frigate"
  echo "sigue leyendo los clips de prueba)."
  read -r -p "IP del grabador en la red del local (o su dirección, si se llega desde afuera) [$(actual FRIGATE_NVR_IP)]: " ip
  if [ -n "$ip" ]; then
    [[ "$ip" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || corta "«$ip» no es una IP ni un nombre. Va como 192.168.1.64."
    poner FRIGATE_NVR_IP "$ip"
    read -r -p "Puerto del grabador para el video (RTSP) [$(actual FRIGATE_NVR_PUERTO)]: " puerto
    if [ -n "$puerto" ]; then
      [[ "$puerto" =~ ^[0-9]{1,5}$ ]] && [ "$puerto" -ge 1 ] && [ "$puerto" -le 65535 ] || corta "«$puerto» no es un puerto. El de siempre es 554."
      poner FRIGATE_NVR_PUERTO "$puerto"
    fi
    read -r -p "Usuario del grabador [$(actual FRIGATE_RTSP_USER)]: " usuario
    [ -z "$usuario" ] || poner FRIGATE_RTSP_USER "$(codificar "$usuario")"
    read -r -s -p "Clave del grabador (no se ve al escribirla)$([ -n "$(actual FRIGATE_RTSP_PASSWORD)" ] && echo ' [la que estaba]'): " clave
    echo
    [ -z "$clave" ] || poner FRIGATE_RTSP_PASSWORD "$(codificar "$clave")"
  fi
  [ -n "$(actual FRIGATE_NVR_PUERTO)" ] || poner FRIGATE_NVR_PUERTO 554
  if [ -n "$(actual FRIGATE_NVR_IP)" ] && grep -q '/media/pruebas/' frigate/config.yml; then
    echo
    echo "Frigate está leyendo los clips de prueba. Las zonas que hayas corregido"
    echo "en su pantalla quedan como están; sólo cambia de dónde lee cada cámara."
    read -r -p "¿Lo paso a leer las cámaras del grabador? (s/n) " r
    if [ "$r" = "s" ]; then
      cp frigate/config.yml "frigate/config.yml.bak-$(date +%Y%m%d-%H%M%S)"
      sed -E -i \
        -e 's#path: /media/pruebas/camara([0-9])\.mp4#path: rtsp://{FRIGATE_RTSP_USER}:{FRIGATE_RTSP_PASSWORD}@{FRIGATE_NVR_IP}:{FRIGATE_NVR_PUERTO}/Streaming/Channels/\102#' \
        -e '/input_args: -re -stream_loop/d' \
        -e '/hwaccel_args: ""/d' frigate/config.yml
      REINICIAR_FRIGATE=si
      bien "Frigate lee del grabador: camara1 el canal 1, camara2 el 2, y así"
    fi
  fi

  echo
  echo "Mandando, lo que cuenta este equipo aparece en el tablero del dueño."
  read -r -p "¿Empieza a mandar al servidor? (s/n) [$([ "$(actual MANDAR)" = "si" ] && echo s || echo n)]: " r
  case "$r" in
    s) poner MANDAR si ;;
    n) poner MANDAR no ;;
    "") ;;
    *) corta "Era s o n." ;;
  esac
  chmod 600 .env
  bien "guardado"
}

if [ "$SOLO_ESTADO" = si ]; then estado; exit 0; fi

[ "$(uname -m)" = "aarch64" ] || corta "Este programa es para la Raspberry Pi (64 bits). Acá hay $(uname -m)."
[ "$(id -u)" -ne 0 ] || corta "Corrélo con tu usuario, sin sudo: pide la clave sólo cuando hace falta."

# ── 1. Docker ───────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  dice "Instalo Docker (con el instalador oficial, get.docker.com)"
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sudo sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
  sudo usermod -aG docker "$USER"
fi
# Recién instalado, este usuario todavía no está en el grupo docker en esta sesión.
if docker info >/dev/null 2>&1; then DOCKER="docker"; else DOCKER="sudo docker"; fi

# ── 2. El controlador del Hailo que pide Frigate (sólo si se pidió) ─────────
if [ "$CONTROLADOR" = si ]; then
  dice "El controlador del Hailo para Frigate"
  echo "Esto reemplaza el controlador que instaló hailo-all. Los ejemplos de"
  echo "~/hailo-apps pueden dejar de andar. Al terminar hay que reiniciar la Pi."
  if [ "$SISTEMA" = "bookworm" ]; then
    echo
    echo "Este sistema es Bookworm: antes hay que desactivar a mano el controlador"
    echo "que trae. Los pasos están en la guía de Frigate, sección Hailo-8:"
    echo "  $DOCS_HAILO"
    echo "Hacelos, reiniciá y volvé a correr ./instalar.sh --controlador"
    read -r -p "¿Ya los hiciste? (s/n) " r
    [ "$r" = "s" ] || corta "Listo, no toqué nada."
  fi
  read -r -p "¿Instalo el controlador de Frigate? (s/n) " r
  [ "$r" = "s" ] || corta "Listo, no toqué nada."
  curl -fsSL "$SCRIPT_HAILO" -o /tmp/hailo_frigate.sh
  chmod +x /tmp/hailo_frigate.sh
  /tmp/hailo_frigate.sh
  rm -f /tmp/hailo_frigate.sh
  dice "Controlador instalado. Reiniciá la Pi (sudo reboot) y corré ./instalar.sh de nuevo."
  exit 0
fi

# ── 3. Las carpetas, el .env y la configuración de Frigate ──────────────────
mkdir -p clips datos almacen
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  dice "Creé el .env en modo de prueba (MANDAR=no): cuenta y no manda nada."
fi
if [ ! -f frigate/config.yml ]; then
  cp frigate/config.ejemplo.yml frigate/config.yml
  bien "la configuración de Frigate, desde la plantilla"
fi

REINICIAR_FRIGATE=no
if [ "$CONFIGURAR" = si ]; then configurar; fi

if [ ! -e /dev/hailo0 ]; then
  estado
  corta "Sin el Hailo, Frigate no arranca. Si la cinta está bien puesta: ./instalar.sh --controlador"
fi

# ── 4. Levantar ─────────────────────────────────────────────────────────────
dice "Bajo Frigate y armo el puente (la primera vez tarda varios minutos)"
$DOCKER compose pull mosquitto frigate
$DOCKER compose build puente
$DOCKER compose up -d
if [ "$REINICIAR_FRIGATE" = si ]; then $DOCKER compose restart frigate; fi

estado

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
dice "Listo. Lo que sigue:"
cat <<FIN
  1. La clave de Frigate sale una sola vez en su registro:
       $DOCKER logs frigate 2>&1 | grep -i password
  2. Abrir la pantalla de Frigate en el navegador de esta misma Pi:
       https://localhost:8971
     o desde otra computadora de la misma red:  https://${IP:-IP-DE-LA-PI}:8971
     (avisa que el certificado no es conocido: es el propio de Frigate)
  3. Lo que va contando el puente, cada minuto:
       $DOCKER compose logs -f puente

  Si Frigate dice que no puede usar el Hailo, falta su controlador:
       ./instalar.sh --controlador
  Para ponerle un local a este equipo (el link de la caja y el PIN):
       ./instalar.sh --configurar
  Para actualizar más adelante:
       git pull && ./instalar.sh
FIN
