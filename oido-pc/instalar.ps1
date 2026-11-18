#Requires -Version 5.1
# ============================================================================
# Oído de la PC de la caja (Autcala)
# ============================================================================
# Deja esta PC capturando su MICRÓFONO, detectando la voz y mandando los tramos
# al servidor de Autcala — igual que el oído del equipo, pero por el micrófono
# de la PC en vez del RTSP de la cámara. Corre como Tarea Programada (arranca
# solo al iniciar sesión), en segundo plano, SIN abrir el navegador.
#
# Reutiliza el mismo oido.py del equipo (lo baja del repo público).
#
# Cómo correrlo (en la PC de la caja), en una ventana de PowerShell:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
#   & "<ruta>\instalar.ps1"
#
# Se puede correr de nuevo para cambiar el micrófono, el local o el PIN.
# Para sacarlo:  Unregister-ScheduledTask -TaskName AutcalaOido -Confirm:$false
# ============================================================================
$ErrorActionPreference = "Stop"
$NOMBRE   = "AutcalaOido"
$DIR      = Join-Path $env:LOCALAPPDATA "autcala-oido"
$OIDO_URL = "https://raw.githubusercontent.com/tinchohan/autcala-equipo/main/oido/oido.py"
$API      = "https://api-bis.autcala.com/api/v1"

function Hay($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function RefrescarPath {
  $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
              [Environment]::GetEnvironmentVariable("Path","User")
}

# Python REAL (no el acceso directo a la Tienda de Windows, que vive en
# ...\WindowsApps y sólo abre la Store). Deja $script:PY_EXE (ruta completa) y
# $script:PY_PRE (prefijo, p.ej. -3 para el lanzador "py").
$script:PY_EXE = $null
$script:PY_PRE = @()
function BuscarPython {
  $script:PY_EXE = $null; $script:PY_PRE = @()
  # 1) el lanzador "py" (no lo pisa el acceso directo de la Tienda)
  $py = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($py) {
    try { & $py.Source -3 --version 2>$null | Out-Null } catch {}
    if ($LASTEXITCODE -eq 0) { $script:PY_EXE = $py.Source; $script:PY_PRE = @("-3"); return $true }
  }
  # 2) python.exe que NO sea el stub de la Tienda
  foreach ($c in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
    if ($c.Source -and $c.Source -notlike "*\WindowsApps\*") {
      $script:PY_EXE = $c.Source; $script:PY_PRE = @(); return $true
    }
  }
  # 3) lugares típicos de instalación
  $cand = Get-ChildItem -ErrorAction SilentlyContinue `
            "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", `
            "C:\Program Files\Python3*\python.exe", `
            "C:\Python3*\python.exe" | Select-Object -First 1
  if ($cand) { $script:PY_EXE = $cand.FullName; $script:PY_PRE = @(); return $true }
  return $false
}
function Py { & $script:PY_EXE @script:PY_PRE @args }

Write-Host "== Oído de la PC de la caja ==" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $DIR | Out-Null
RefrescarPath

# 1. Python ------------------------------------------------------------------
if (-not (BuscarPython)) {
  Write-Host "Instalando Python (puede tardar unos minutos)..."
  if (Hay winget) {
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    RefrescarPath
    BuscarPython | Out-Null
  }
}
if (-not $script:PY_EXE) {
  throw "No pude instalar ni encontrar Python real. Instalá Python de https://python.org (marcá 'Add python.exe to PATH'), cerrá y abrí PowerShell, y volvé a correr este instalador."
}
Write-Host "Python: $($script:PY_EXE) $($script:PY_PRE -join ' ')" -ForegroundColor Green

# 2. ffmpeg ------------------------------------------------------------------
if (-not (Hay ffmpeg)) {
  Write-Host "Instalando ffmpeg (puede tardar unos minutos)..."
  if (Hay winget) {
    winget install -e --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
    RefrescarPath
  } else {
    throw "Falta ffmpeg y no hay winget. Instalá ffmpeg y agregalo al PATH, y volvé a correr."
  }
}
if (-not (Hay ffmpeg)) {
  throw "ffmpeg no quedó en el PATH. Cerrá y abrí PowerShell (o reiniciá) y volvé a correr."
}

# 3. Detector de voz ---------------------------------------------------------
Write-Host "Instalando la librería de detección de voz..."
Py -m pip install --quiet --upgrade pip
Py -m pip install --quiet "webrtcvad-wheels>=2.0,<3"

# 4. El oído -----------------------------------------------------------------
Write-Host "Descargando el oído..."
Invoke-WebRequest -Uri $OIDO_URL -OutFile (Join-Path $DIR "oido.py") -UseBasicParsing

# 5. Elegir el micrófono -----------------------------------------------------
# ffmpeg escribe la lista de dispositivos por el canal de errores; con
# ErrorActionPreference=Stop eso abortaría el script. Por eso corremos por
# cmd /c con 2>&1 (mezcla al nivel de cmd) y PowerShell nunca ve "errores".
Write-Host ""
Write-Host "Micrófonos de esta PC:" -ForegroundColor Yellow
$ffmpegExe = (Get-Command ffmpeg).Source
$salida = (cmd /c "`"$ffmpegExe`" -hide_banner -list_devices true -f dshow -i dummy 2>&1") | Out-String
$mics = [regex]::Matches($salida, '"([^"]+)"\s*\(audio\)') | ForEach-Object { $_.Groups[1].Value }
if ($mics.Count -eq 0) {
  $mics = [regex]::Matches($salida, '"([^"@][^"]*)"') | ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique
}
for ($i = 0; $i -lt $mics.Count; $i++) { Write-Host ("  [{0}] {1}" -f $i, $mics[$i]) }
if ($mics.Count -eq 0) {
  Write-Host "  (no detecté ninguno automáticamente; pegá el nombre exacto del micrófono)" -ForegroundColor DarkYellow
  Write-Host "  --- lo que devolvió ffmpeg, por si ayuda: ---" -ForegroundColor DarkGray
  Write-Host $salida -ForegroundColor DarkGray
}
$sel = Read-Host "Número del micrófono de la caja (o pegá el nombre exacto, sin comillas)"
if ($sel -match '^\d+$' -and [int]$sel -lt $mics.Count) { $MIC = $mics[[int]$sel] } else { $MIC = $sel.Trim() }
if (-not $MIC) { throw "No elegiste un micrófono." }
Write-Host "Micrófono: $MIC" -ForegroundColor Green

# 6. Local y PIN -------------------------------------------------------------
$LOCAL = Read-Host "Link de la caja del panel (pego entero o sólo el ID del final)"
$LOCAL = ($LOCAL -split '[/?]' | Where-Object { $_ -match '^[0-9a-fA-F-]{20,}$' } | Select-Object -Last 1)
if (-not $LOCAL) { throw "No reconocí el ID del local en lo que pegaste." }
$PINsec = Read-Host "PIN de la caja (no se ve al escribirlo)" -AsSecureString
$PIN = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
         [Runtime.InteropServices.Marshal]::SecureStringToBSTR($PINsec))
if (-not $PIN) { throw "El PIN no puede estar vacío." }

# 7. El .env (restringido a este usuario) ------------------------------------
$envPath = Join-Path $DIR ".env"
@(
  "LOCAL_ID=$LOCAL"
  "PIN=$PIN"
  "OIDO=si"
  "MANDAR=si"
  "OIDO_FUENTE=mic"
  "OIDO_MIC=$MIC"
  "OIDO_DENOISE=no"
  "API=$API"
) -join "`r`n" | Set-Content -Path $envPath -Encoding UTF8
# El grupo de administradores por su SID (*S-1-5-32-544), independiente del
# idioma de Windows ("Administrators"/"Administradores").
icacls $envPath /inheritance:r /grant:r "$($env:USERNAME):(R,W)" "*S-1-5-32-544:(F)" | Out-Null

# 8. El envoltorio que carga el .env y corre el oído (oculto, con registro) ---
# Se le incrusta la ruta real de Python para que la tarea no dependa del PATH
# ni tropiece con el acceso directo de la Tienda.
$pyCall = '& "' + $script:PY_EXE + '"'
if ($script:PY_PRE.Count) { $pyCall += ' ' + ($script:PY_PRE -join ' ') }
$pyCall += ' "$PSScriptRoot\oido.py"'
$correr = @"
`$ErrorActionPreference = "SilentlyContinue"
Set-Location `$PSScriptRoot
Get-Content "`$PSScriptRoot\.env" | Where-Object { `$_ -match "^\s*[^#].*=" } | ForEach-Object {
  `$p = `$_ -split "=", 2
  [Environment]::SetEnvironmentVariable(`$p[0].Trim(), `$p[1].Trim(), "Process")
}
$pyCall 2>&1 | Out-File -FilePath "`$PSScriptRoot\oido.log" -Append -Encoding UTF8
"@
Set-Content -Path (Join-Path $DIR "correr.ps1") -Value $correr -Encoding UTF8

# 9. Arranque automático al iniciar sesión ----------------------------------
# Primero intenta una Tarea Programada (se reinicia sola si cae). Si el usuario
# no es administrador, Windows niega el registro: ahí caemos a un atajo en la
# carpeta de Inicio, que no necesita permisos.
$correrFile = Join-Path $DIR "correr.ps1"
$arg = "-WindowStyle Hidden -NonInteractive -ExecutionPolicy Bypass -File `"$correrFile`""
$porTarea = $false
try {
  $accion   = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg
  $trigger  = New-ScheduledTaskTrigger -AtLogOn
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName $NOMBRE -Action $accion -Trigger $trigger -Settings $settings `
                -RunLevel Limited -Force -ErrorAction Stop | Out-Null
  Start-ScheduledTask -TaskName $NOMBRE
  $porTarea = $true
} catch {
  Write-Host "No pude registrar la Tarea Programada ($($_.Exception.Message))." -ForegroundColor DarkYellow
  Write-Host "Uso el arranque por la carpeta de Inicio (no necesita administrador)." -ForegroundColor DarkYellow
  $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) "AutcalaOido.lnk"
  $ws = New-Object -ComObject WScript.Shell
  $sc = $ws.CreateShortcut($lnk)
  $sc.TargetPath = "powershell.exe"
  $sc.Arguments  = $arg
  $sc.WorkingDirectory = $DIR
  $sc.WindowStyle = 7  # minimizado
  $sc.Save()
  # Y lo arrancamos ahora (la tarea lo habría hecho sola)
  Start-Process powershell -WindowStyle Hidden -ArgumentList $arg
}

Write-Host ""
Write-Host "Listo. El oído corre en segundo plano y arranca solo al iniciar sesión." -ForegroundColor Green
Write-Host "  Carpeta:   $DIR"
Write-Host "  Registro:  $DIR\oido.log   (si algo falla, mirá acá)"
if ($porTarea) {
  Write-Host "  Arranque:  Tarea Programada '$NOMBRE'"
  Write-Host "  Frenarlo:  Unregister-ScheduledTask -TaskName $NOMBRE -Confirm:`$false"
} else {
  Write-Host "  Arranque:  atajo en la carpeta de Inicio (AutcalaOido.lnk)"
  Write-Host "  Frenarlo:  cerrá el proceso de python y borrá el atajo de Inicio (shell:startup)"
}
Write-Host ""
Write-Host "Dale unos segundos y fijate el registro para confirmar que arrancó:" -ForegroundColor Cyan
Write-Host "  Get-Content `"$DIR\oido.log`" -Tail 10 -Wait"
