#!/usr/bin/env bash
# Vorbereitung fuer tools/probetest_sps.py im Container: eigene Python-Umgebung mit asyncua (OPC UA).
# Aendert nichts an der laufenden Simulation; grpcio/protobuf kommen aus den Systempaketen.
#
#   sudo bash /opt/rebel-sim/tools/probetest_vorbereiten.sh
#   sudo -u rebelsim /opt/rebel-sim-probe/bin/python3 /opt/rebel-sim/tools/probetest_sps.py opcua --sps <IP>
set -euo pipefail
ZIEL="${ZIEL:-/opt/rebel-sim-probe}"
apt-get install -y -q python3-venv python3-cryptography >/dev/null
[ -x "$ZIEL/bin/python3" ] || python3 -m venv --system-site-packages "$ZIEL"
"$ZIEL/bin/pip" install -q --upgrade asyncua
install -d -o rebelsim -g rebelsim -m 700 /var/lib/rebel-sim/opcua   # eigenes Zertifikat fuer OPC UA
"$ZIEL/bin/python3" -c "import asyncua, grpc, google.protobuf; print('asyncua', asyncua.__version__, '/ grpc', grpc.__version__)"
echo "Fertig. Aufruf: sudo -u rebelsim $ZIEL/bin/python3 /opt/rebel-sim/tools/probetest_sps.py --help"
