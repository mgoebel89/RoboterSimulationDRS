"""Vorwaertskinematik und einfache Rueckwaertskinematik des ReBeL 6DOF,
passend zum Webots-Modell (Masse aus REBEL-6DOF-03.xml, in Metern).

Nur fuer den Demo-Server und die Tests; im Betrieb rechnet iRC.
"""

import math

LZ0, LZ1, LX2, LX3, LX4, LX6 = 0.100, 0.1522, 0.24152, 0.1222, 0.1778, 0.129
FLANSCH_DICKE = 0.0012
TCP_EGP40 = 0.103          # Schunk EGP 40 mit ReBeL-Adapterplatte, wie in iRC (Standardgreifer)
TCP_EGP25 = 0.085          # Schunk EGP 25 mit ReBeL-Adapterplatte, wie in iRC
TCP_EGP25_FINGER = 0.105   # dasselbe mit den iRC-Standardfingern
TCP_EGP25_ROBI2 = 0.1307   # mit den Robi2-Backen, Backenspitze (Lage provisorisch, cad/backen/robi2)
TCP_VAKUUM = FLANSCH_DICKE + 0.072


def _rz(q):
    c, s = math.cos(q), math.sin(q)
    return [c, -s, 0, s, c, 0, 0, 0, 1]


def _ry(q):
    c, s = math.cos(q), math.sin(q)
    return [c, 0, s, 0, 1, 0, -s, 0, c]


def _mul(a, b):
    return [sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3)) for r in range(3) for c in range(3)]


def _vec(a, v):
    return [a[r * 3] * v[0] + a[r * 3 + 1] * v[1] + a[r * 3 + 2] * v[2] for r in range(3)]


def fk(q_deg, tool=0.0):
    """Gelenkwinkel in Grad -> (Position, Rotationsmatrix) von Flansch+tool."""
    q = [math.radians(v) for v in q_deg]
    rots = [_rz, _ry, _ry, _rz, _ry, _rz]
    offs = [(0, 0, LZ0), (0, 0, LZ1), (0, 0, LX2), (0, 0, LX3), (0, 0, LX4), (0, 0, LX6)]
    p = [0.0, 0.0, 0.0]
    r = [1, 0, 0, 0, 1, 0, 0, 0, 1]
    for rot, off, qi in zip(rots, offs, q):
        d = _vec(r, off)
        p = [p[i] + d[i] for i in range(3)]
        r = _mul(r, rot(qi))
    d = _vec(r, (0, 0, tool))
    return [p[i] + d[i] for i in range(3)], r


def ik_nach_unten(x, y, z, tool):
    """Gelenkwinkel (Grad), damit der TCP mit senkrecht nach unten zeigendem
    Werkzeug bei (x, y, z) steht. Ellbogen oben."""
    q1 = math.atan2(y, x)
    r = math.hypot(x, y)
    dx = r
    dz = z + LX6 + tool - (LZ0 + LZ1)
    l1, l2 = LX2, LX3 + LX4
    c = (dx * dx + dz * dz - l1 * l1 - l2 * l2) / (2 * l1 * l2)
    if abs(c) > 1:
        raise ValueError("Punkt ausserhalb der Reichweite")
    b = math.acos(c)
    a = math.atan2(dx, dz) - math.atan2(l2 * math.sin(b), l1 + l2 * math.cos(b))
    q5 = math.pi - a - b
    return [math.degrees(v) for v in (q1, a, b, 0.0, q5, 0.0)]
