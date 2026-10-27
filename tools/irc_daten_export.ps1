# Packt die iRC-Dateien, aus denen der Container das Robotermodell erzeugt, in irc-daten.zip:
#   Data\Robots\REBEL\REBEL-6DOF-03\*        (Kinematik und Glieder des igus ReBeL)
#   Data\Tools\Schunk_EGP40_*, Schunk_EGP25_* (Greifer Schunk EGP 40 und 25 mit ReBeL-Adapterplatte)
# Die Dateien gehoeren igus und duerfen nicht weitergegeben werden; deshalb stehen sie nicht im
# Repository, sondern kommen nur in den eigenen Container (Einrichtungsassistent im Browser oder
# rebel-sim-modell /root/irc-daten.zip).
#
# Am einfachsten: im Einrichtungsassistenten "irc-daten-export.cmd" herunterladen und doppelklicken (dasselbe Skript).
# Aufruf in PowerShell auf einem PC mit igus Robot Control V15:
#   powershell -ExecutionPolicy Bypass -File irc_daten_export.ps1
#   powershell -ExecutionPolicy Bypass -File irc_daten_export.ps1 -Irc "D:\iRC-igusRobotControl-V15" -Ziel C:\Temp\irc-daten.zip
param(
    [string]$Irc = "C:\Program Files\iRC-igusRobotControl-V15",
    [string]$Ziel = (Join-Path ([Environment]::GetFolderPath("Desktop")) "irc-daten.zip")
)
$ErrorActionPreference = "Stop"
$pfad = "Data\Robots\REBEL\REBEL-6DOF-03\REBEL-6DOF-03.xml"
if (-not (Test-Path (Join-Path $Irc $pfad))) {
    # anders installiert (andere Version im Ordnernamen, Laufwerk D:)? In den ueblichen Ordnern suchen
    $gefunden = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, "C:\", "D:\") | Where-Object { $_ -and (Test-Path $_) } |
        ForEach-Object { Get-ChildItem $_ -Directory -Filter "iRC*" -ErrorAction SilentlyContinue } |
        Where-Object { Test-Path (Join-Path $_.FullName $pfad) } | Select-Object -First 1
    if (-not $gefunden) {
        throw "iRC V15 nicht gefunden ($Irc\$pfad fehlt). In PowerShell mit -Irc den Installationsordner angeben."
    }
    $Irc = $gefunden.FullName
}
Write-Host "iRC: $Irc"
$roboter = Join-Path $Irc "Data\Robots\REBEL\REBEL-6DOF-03"
$werkzeuge = Join-Path $Irc "Data\Tools"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("irc-daten-" + [Guid]::NewGuid())
New-Item -ItemType Directory -Force (Join-Path $tmp "Data\Robots\REBEL\REBEL-6DOF-03") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $tmp "Data\Tools") | Out-Null
Copy-Item (Join-Path $roboter "*") (Join-Path $tmp "Data\Robots\REBEL\REBEL-6DOF-03")
Copy-Item (Join-Path $werkzeuge "Schunk_EGP25_*") (Join-Path $tmp "Data\Tools")
Copy-Item (Join-Path $werkzeuge "Schunk_EGP40_*") (Join-Path $tmp "Data\Tools")
if (Test-Path $Ziel) { Remove-Item $Ziel }
Compress-Archive -Path (Join-Path $tmp "Data") -DestinationPath $Ziel
Remove-Item -Recurse -Force $tmp
Write-Host ""
Write-Host "Fertig: $Ziel"
Write-Host "Diese Datei jetzt im Einrichtungsassistenten der Simulation hochladen (Schritt Robotermodell)."
Write-Host "Ohne Browser: in den Container kopieren und dort  rebel-sim-modell /root/irc-daten.zip"
