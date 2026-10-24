# Packt die iRC-Dateien, aus denen der Container das Robotermodell erzeugt, in irc-daten.zip:
#   Data\Robots\REBEL\REBEL-6DOF-03\*        (Kinematik und Glieder des igus ReBeL)
#   Data\Tools\Schunk_EGP25_*                (Greifer Schunk EGP 25 mit ReBeL-Adapterplatte)
# Die Dateien gehoeren igus und duerfen nicht weitergegeben werden; deshalb stehen sie nicht im
# Repository, sondern kommen nur in den eigenen Container (rebel-sim-modell /root/irc-daten.zip).
#
# Aufruf in PowerShell auf einem PC mit igus Robot Control V15:
#   powershell -ExecutionPolicy Bypass -File irc_daten_export.ps1
#   powershell -ExecutionPolicy Bypass -File irc_daten_export.ps1 -Irc "D:\iRC-igusRobotControl-V15" -Ziel C:\Temp\irc-daten.zip
param(
    [string]$Irc = "C:\Program Files\iRC-igusRobotControl-V15",
    [string]$Ziel = (Join-Path ([Environment]::GetFolderPath("Desktop")) "irc-daten.zip")
)
$ErrorActionPreference = "Stop"
$roboter = Join-Path $Irc "Data\Robots\REBEL\REBEL-6DOF-03"
$werkzeuge = Join-Path $Irc "Data\Tools"
if (-not (Test-Path (Join-Path $roboter "REBEL-6DOF-03.xml"))) {
    throw "iRC nicht gefunden: $roboter fehlt. Mit -Irc den Installationsordner angeben."
}
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("irc-daten-" + [Guid]::NewGuid())
New-Item -ItemType Directory -Force (Join-Path $tmp "Data\Robots\REBEL\REBEL-6DOF-03") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $tmp "Data\Tools") | Out-Null
Copy-Item (Join-Path $roboter "*") (Join-Path $tmp "Data\Robots\REBEL\REBEL-6DOF-03")
Copy-Item (Join-Path $werkzeuge "Schunk_EGP25_*") (Join-Path $tmp "Data\Tools")
if (Test-Path $Ziel) { Remove-Item $Ziel }
Compress-Archive -Path (Join-Path $tmp "Data") -DestinationPath $Ziel
Remove-Item -Recurse -Force $tmp
Write-Host "Fertig: $Ziel"
Write-Host "In den Container kopieren, z.B.:  scp `"$Ziel`" root@<container>:/root/"
Write-Host "Dann im Container:                rebel-sim-modell /root/irc-daten.zip"
