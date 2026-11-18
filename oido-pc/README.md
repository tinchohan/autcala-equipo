# El oído en la PC de la caja

Deja la **PC de la caja** (la de facturación) capturando su **micrófono**,
detectando la voz y mandando los tramos al servidor de Autcala — **sin abrir el
navegador**, como un servicio que arranca solo.

Es el mismo oído que corre en el equipo del local, pero por el **micrófono de la
PC** en vez del RTSP de la cámara. ¿Por qué? Porque el micrófono de la PC está
**sobre el mostrador**, pegado a las voces: capta mucho mejor que una cámara
lejana (la voz llega fuerte y limpia, con buena relación señal/ruido). Reutiliza
el mismo `oido.py` del equipo (`OIDO_FUENTE=mic`), que lo baja del repo público.

## Qué hace

- Escucha el micrófono con **ffmpeg**, detecta la voz (webrtcvad) y manda al
  servidor **sólo los tramos con voz**, por la misma puerta que la caja (el PIN).
- El servidor los junta, los transcribe y arma los resúmenes de cada ticket y el
  monto sin ticket. **De la PC no sale nada más que el audio con voz.**
- Corre como **Tarea Programada** (al iniciar sesión), en segundo plano. Si se
  cae, se reinicia solo. Si se corta internet, reintenta.

## Instalar

En la PC de la caja (Windows 10/11, con internet y un micrófono):

1. Copiá `instalar.ps1` a la PC.
2. Clic derecho → **"Ejecutar con PowerShell"** (o `powershell -ExecutionPolicy Bypass -File instalar.ps1`).
3. Te va a:
   - instalar **Python** y **ffmpeg** si faltan (por `winget`),
   - **listar los micrófonos** → elegís el de la caja (número o nombre),
   - pedir el **link de la caja** y el **PIN** (el PIN no se ve al escribirlo),
   - dejar todo corriendo como tarea.

Se puede correr de nuevo para cambiar el micrófono, el local o el PIN.

## Confirmar que anda

El registro está en `%LOCALAPPDATA%\autcala-oido\oido.log`. Para verlo en vivo:

```powershell
Get-Content "$env:LOCALAPPDATA\autcala-oido\oido.log" -Tail 10 -Wait
```

Al arrancar tiene que decir algo como *"oído del local … por el micrófono de la
PC"*. Desde el lado de Autcala se ve que llegan los tramos de audio al oído del
servidor.

## Cosas a saber

- **El PIN y el `.env`** viven en `%LOCALAPPDATA%\autcala-oido\.env`, con permisos
  restringidos a este usuario. No se suben a ningún lado.
- **Un solo oído.** Si esta PC ya tenía el oído por la URL de la caja abierta en
  el navegador, cerralo: que escuche **sólo** este agente, o se mezclan.
- **Qué micrófono.** Elegí el que esté sobre el mostrador. Cuanto más cerca de
  las voces, mejor transcribe.
- **Limpieza de ruido (opcional).** En el `.env`, `OIDO_DENOISE=si` aplica un
  filtro de ffmpeg. Con un micrófono de cerca casi no hace falta.
- **Transcripción local (a futuro).** Hoy manda el audio al servidor, que
  transcribe (Whisper). Más adelante se puede transcribir en la PC con un modelo
  local y sacar ese costo, aprovechando que el audio de cerca es limpio.

## Actualizar / sacar

- **Actualizar el oído:** volvé a correr `instalar.ps1` (rebaja `oido.py`).
- **Sacarlo:** `Unregister-ScheduledTask -TaskName AutcalaOido -Confirm:$false`
  y borrá la carpeta `%LOCALAPPDATA%\autcala-oido`.
