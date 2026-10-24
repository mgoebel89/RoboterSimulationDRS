// Vorschaubilder der Teilebibliothek: rendert ein Teil aus seinem Netz (netz.json) schraeg von oben und
// speichert es als teile/<name>/vorschau.png im Container. Genutzt vom Lehrerbereich (Reiter Teile) und vom
// Baukasten (Dialog "Teil hinzufuegen"). Ein gemeinsamer Renderer, Auftraege nacheinander.
import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js';

const BREITE = 320, HOEHE = 240;
let renderer = null;
let kette = Promise.resolve();

function szeneFuer(netz) {
  const pos = [], idx = [];
  for (const f of Object.values(netz)) {
    const basis = pos.length / 3;
    for (const v of f.p) pos.push(v);
    for (const i of f.i) idx.push(i + basis);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geo.setIndex(idx); geo.computeVertexNormals(); geo.computeBoundingSphere();
  const szene = new THREE.Scene();
  szene.add(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({color: 0xa9aeb5, roughness: 0.5, metalness: 0.25})));
  szene.add(new THREE.LineSegments(new THREE.EdgesGeometry(geo, 25),
    new THREE.LineBasicMaterial({color: 0x1a1a1a, transparent: true, opacity: 0.4})));
  szene.add(new THREE.HemisphereLight(0xffffff, 0x8a8f99, 1.7));
  const sonne = new THREE.DirectionalLight(0xffffff, 1.7); sonne.position.set(-1, -1.4, 2.2); szene.add(sonne);
  const sonne2 = new THREE.DirectionalLight(0xffffff, 0.5); sonne2.position.set(1.5, 1, 0.6); szene.add(sonne2);
  // Kamera schraeg von vorne rechts oben, z oben wie in iRC und im Baukasten
  const k = geo.boundingSphere, kamera = new THREE.PerspectiveCamera(30, BREITE / HOEHE, 0.1, 100000);
  kamera.up.set(0, 0, 1);
  const abstand = k.radius / Math.sin(THREE.MathUtils.degToRad(30) / 2) * 1.08;
  kamera.position.copy(k.center).add(new THREE.Vector3(1, -1.25, 0.95).normalize().multiplyScalar(abstand));
  kamera.lookAt(k.center);
  return {szene, kamera, geo};
}

async function rendern(name) {
  const netz = await fetch('/api/lehrer/netz?name=' + encodeURIComponent(name), {cache: 'no-store'}).then(r => {
    if (!r.ok) throw new Error('Netz von ' + name + ' fehlt'); return r.json(); });
  if (!renderer) {
    renderer = new THREE.WebGLRenderer({antialias: true, alpha: true, preserveDrawingBuffer: true});
    renderer.setPixelRatio(1); renderer.setSize(BREITE, HOEHE, false);
  }
  const {szene, kamera, geo} = szeneFuer(netz);
  renderer.setClearColor(0xffffff, 0);
  renderer.render(szene, kamera);
  const bild = await new Promise(fertig => renderer.domElement.toBlob(fertig, 'image/png'));
  geo.dispose();
  const r = await fetch('/api/lehrer/teil_vorschau?name=' + encodeURIComponent(name), {method: 'POST', body: bild,
    headers: {'Content-Type': 'image/png', 'X-Lehrer': '1'}});
  if (!r.ok) throw new Error('Vorschaubild von ' + name + ' nicht gespeichert');
  return URL.createObjectURL(bild);
}

// Vorschau erzeugen und speichern; Rueckgabe: Bildadresse (Objekt-URL). Auftraege laufen nacheinander.
export function vorschauErzeugen(name) {
  const auftrag = kette.then(() => rendern(name));
  kette = auftrag.catch(() => {});
  return auftrag;
}

// Bildadresse eines Teils aus der Liste (/api/lehrer/teile): gespeichertes Bild, sonst jetzt erzeugen
export function vorschauAdresse(t) {
  if (t.vorschau) return Promise.resolve('/api/lehrer/teil_vorschau?name=' + encodeURIComponent(t.name) + '&v=' + t.vorschau);
  return vorschauErzeugen(t.name);
}
