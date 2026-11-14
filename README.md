# El equipo del local de Comercios por Autcala

Lo que corre en una Raspberry Pi 5 con el AI HAT+ (Hailo-8) dentro de cada
local: Frigate mira las cámaras que el local ya tiene y un programa chico,
el puente, lleva la cuenta y la manda al servidor de Autcala. El dueño lo ve
en su tablero.

**De este equipo no sale ninguna imagen.** Frigate no graba ni saca fotos
(el grabador del local ya graba) y el puente manda sólo cuentas, duraciones y
horas. Cuenta personas; no las reconoce.

En el local el equipo va sin monitor ni teclado: enchufado a la corriente y
con un cable de red al router. Se prepara una vez, antes de llevarlo.

**Estado: escrito y probado en una PC; todavía no corrió en una Pi.** El
puente tiene sus pruebas (`puente/test_puente.py`). La configuración de
Frigate sale de su documentación (versión 0.18) y de clips de las cámaras de
un local; la primera vez que arranque en una Pi puede haber algo para tocar.

## Qué hay acá

| Archivo | Qué es |
|---|---|
| `instalar.sh` | El instalador: deja todo andando y configura el equipo para un local |
| `docker-compose.yml` | Levanta las tres piezas: Frigate, el correo interno (MQTT) y el puente |
| `frigate/config.ejemplo.yml` | La plantilla de Frigate: las cámaras, las zonas (puerta, salón, fila, caja) y las máscaras de la calle |
| `puente/puente.py` | El puente: escucha a Frigate, cuenta, guarda en disco y manda |
| `puente/test_puente.py` | Sus pruebas: `python -X utf8 test_puente.py` |
| `.env.example` | Lo que es de cada equipo (se copia a `.env`) |

De cada equipo, y fuera del repositorio: `.env` (el local, el PIN, el
grabador) y `frigate/config.yml` (Frigate guarda ahí las zonas que se
corrigen en su pantalla). Por eso actualizar con `git pull` no los pisa.

## Instalar

Con la Pi conectada a un monitor y un teclado, como una computadora más,
abrir la terminal y escribir:

```
git clone https://github.com/tinchohan/autcala-equipo.git
cd autcala-equipo
./instalar.sh
```

Instala Docker si falta, crea el `.env` en modo de prueba (`MANDAR=no`:
cuenta pero no manda nada), baja Frigate, arma el puente y levanta todo. La
primera vez tarda varios minutos. Al final dice cómo entrar a la pantalla de
Frigate y cómo ver lo que cuenta el puente.

Se puede correr las veces que haga falta. `./instalar.sh --estado` sólo dice
cómo está cada cosa.

La pantalla de Frigate se abre en el navegador de la misma Pi, en
`https://localhost:8971`, o desde otra computadora de la misma red, en
`https://IP-DE-LA-PI:8971`.

(Si se prefiere no conectarle monitor, todo esto se puede hacer igual desde
otra computadora, entrando por SSH: `ssh usuario@IP-DE-LA-PI`. El SSH se
activa al grabar la tarjeta con Raspberry Pi Imager.)

### El controlador del Hailo

Frigate necesita su propia versión del controlador del Hailo. Instalarla
reemplaza la que pone `hailo-all`, así que **los ejemplos de `hailo-apps`
pueden dejar de andar**. Por eso el instalador no lo hace solo:

```
./instalar.sh --controlador
```

Pregunta antes de tocar nada y al terminar pide reiniciar la Pi. En
Bookworm (`cat /etc/os-release`) hay que desactivar antes, a mano, el
controlador que trae el sistema: los pasos están en la guía oficial, sección
Hailo-8, https://docs.frigate.video/frigate/installation

### Probar con clips, antes de ir al local

La plantilla arranca leyendo un clip por cámara, que se repite como si fuera
la cámara. Copiarlos a la carpeta `clips/` con estos nombres:

| Archivo | Qué ve esa cámara |
|---|---|
| `camara1.mp4` | El mostrador, desde donde se empieza a pedir. Es la que cuenta |
| `camara2.mp4` | Desde atrás de la caja, mirando a la puerta. Mide el segundo mostrador, si lo hay |
| `camara3.mp4` | El salón desde el fondo, con la vereda por los vidrios |
| `camara4.mp4` | La cocina |

Si falta alguno, Frigate muestra esa cámara caída y las otras siguen. Con
los cuatro clips en alta resolución la Pi trabaja más que en el local, donde
lee el canal liviano de cada cámara.

Qué mirar en la pantalla de Frigate:

- **Que detecte.** En la vista de la cámara, modo «Debug», con «Bounding
  boxes» y «Zones»: cada persona con su recuadro, y las zonas dibujadas.
- **Que use el Hailo.** En «System metrics», el detector tiene que decir
  unos pocos milisegundos por imagen.
- **Las zonas.** En Ajustes → «Masks / Zones» se arrastran los puntos. Lo
  que cuenta es dónde caen LOS PIES (el centro de abajo del recuadro). La
  gente de la calle no tiene que tener recuadro; la de atrás del mostrador
  sí, pero fuera de toda zona.
- **Dos mostradores.** La cámara 1 mide el mostrador principal y la 2, el
  segundo (una heladería, por ejemplo): cada una con su `fila` y su `caja`.
  La puerta y el salón van dibujados en una sola cámara, para no contar dos
  veces a la misma persona. Si el local tiene un solo mostrador, se le
  borran las dos zonas a la cámara 2.
- **La calle.** Las cámaras 2 y 3 ven la vereda por la puerta y los vidrios.
  Si alguien que pasa por afuera queda con recuadro, hay que agrandar la
  máscara de esa cámara: con el local cerrado sería un aviso falso.
- **Lo que cuenta el puente.** Cada minuto escribe lo que hubiera mandado:
  `docker compose logs -f puente`. Con un clip que se repite los números no
  significan nada: sirve para ver que entra, espera y caja se mueven.

## Ponerle un local

```
./instalar.sh --configurar
```

Pregunta, de a una cosa:

1. **El link de la caja del local** (en el panel de Autcala, «Link de la
   caja»). De ahí sale qué local es.
2. **El PIN de esa caja.** No se ve al escribirlo. El equipo entra al
   servidor igual que la caja: con ese PIN.
3. **El horario del local**, para avisar si se ve a alguien con el local
   cerrado.
4. **El grabador de las cámaras**: su IP en la red del local, el usuario y
   la clave. Con eso Frigate deja los clips y pasa a leer las cámaras
   (camara1 el canal 1 del grabador, camara2 el 2, y así). Las zonas quedan
   como estaban. Si se deja la IP vacía, sigue con los clips.
5. **Si empieza a mandar.** Con «s», lo que cuenta aparece en el tablero del
   dueño.

Se puede correr de nuevo para cambiar una sola cosa: Enter deja lo que
estaba. Lo deja guardado en el `.env` del equipo, que sólo puede leer su
usuario.

En el local alcanza con enchufar el equipo a la corriente y al router: al
prender arranca solo, sin monitor. Para corregir zonas más adelante hay que
entrar a la pantalla de Frigate desde una computadora de esa misma red.

## Qué manda

Una vez por minuto, al servidor de Autcala y con el PIN de la caja:

- **Tramos de 15 minutos**: cuántas personas entraron, cuántas hubo a la vez
  en el salón y en la fila, cuántas llegaron a la caja y cuánto tardaron
  desde que empezaron a pedir, y cuántas cámaras está viendo.
- **Eventos con su hora**: alguien parado en la caja, y alguien en el local
  cuando está cerrado.

```json
{
  "tramos": [
    {"desde": "2026-11-13T16:00:00+00:00", "entraron": 7, "en_salon": 5, "en_fila": 3,
     "esperas": 4, "espera_s": 520, "espera_max_s": 310, "esperas_largas": 1, "camaras": 4}
  ],
  "eventos": [
    {"clave": "caja-1731514320.52-ab12cd", "tipo": "en_caja", "camara": 1,
     "desde": "2026-11-13T16:02:10+00:00", "segundos": 48}
  ]
}
```

Cómo se cuenta cada cosa está explicado al principio de `puente/puente.py`.

## Cosas a saber

- **Las cámaras que se mueven.** Las zonas y las máscaras son dibujos fijos
  sobre la imagen: valen para una posición de la cámara. Se puede acomodar
  la cámara para que coincida con el dibujo en vez de corregir el dibujo,
  pero una vez que coincide no hay que moverla. Si la cámara 1 se mueve,
  cuenta mal sin avisar; si se mueve una que tiene la calle tapada, la calle
  queda a la vista y de noche aparecen avisos falsos. Conviene guardar esa
  posición como punto fijo en la app de la cámara y volver siempre a él.
  Una cámara sin zonas ni máscaras (la cocina) se puede mover sin problema.
- **El PIN.** Si el servidor lo rechaza, el puente espera 15 minutos antes
  de probar de nuevo, para no trabarle la caja al local. Si se cambia el PIN
  en el panel, hay que cambiarlo acá: `./instalar.sh --configurar`.
- **El reloj.** La hora de cada cosa es la de la Pi, no la que la cámara
  escribe sobre la imagen. La Pi toma la hora de internet.
- **Sin internet.** Sigue contando, guarda en el disco y manda todo cuando
  vuelve (hasta 7 días de atraso).
- **Un corte de luz.** Al volver arranca solo y sigue.
- **Actualizar.** `git pull && ./instalar.sh`.
- **El audio** de las cámaras todavía no pasa por acá.

Todos los derechos reservados.
