import { useEffect, useRef } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import type { PartGeometry, PartView } from '../api';

/**
 * The 3D half of Parts. Deliberately an adapter, not a scene owner:
 * geometry and a serializable `PartView` go in; pixels come out. Agent view
 * commands and the manual controls both change the same props, so neither
 * path depends on three.js internals.
 */
type Props = {
  geometry: PartGeometry | null;
  view?: PartView;
  compare?: PartGeometry | null;
  resetToken?: number;
  onUserCamera?: () => void;
  className?: string;
};

const BASE = new THREE.Color('#8b93a7');
const OVERHANG = new THREE.Color('#fb7185');
const THIN = new THREE.Color('#fbbf24');
const GHOST = new THREE.Color('#38bdf8');

function centerAndSize(positions: number[]) {
  const box = new THREE.Box3();
  const v = new THREE.Vector3();
  for (let i = 0; i < positions.length; i += 3) {
    v.set(positions[i], positions[i + 1], positions[i + 2]);
    box.expandByPoint(v);
  }
  const center = new THREE.Vector3();
  const size = new THREE.Vector3();
  box.getCenter(center);
  box.getSize(size);
  return { center, size, radius: Math.max(size.length() / 2, 0.001) };
}

function cameraPosition(preset: string, center: THREE.Vector3,
                        radius: number): THREE.Vector3 {
  const dist = radius * 3.2;
  const dirs: Record<string, [number, number, number]> = {
    front: [0, -1, 0.15],
    back: [0, 1, 0.15],
    left: [-1, 0, 0.15],
    right: [1, 0, 0.15],
    top: [0.001, 0, 1],
    bottom: [0.001, 0, -1],
    iso: [1, -1, 0.8],
    fit: [1, -1, 0.8],
  };
  const [x, y, z] = dirs[preset] || dirs.iso;
  return new THREE.Vector3(x, y, z).normalize().multiplyScalar(dist).add(center);
}

function faceOverlayColors(positions: number[], indices: number[],
                           overlay: string): Float32Array | null {
  if (overlay === 'none') return null;
  const triCount = Math.floor(indices.length / 3);
  const colors = new Float32Array(triCount * 9);
  const a = new THREE.Vector3();
  const b = new THREE.Vector3();
  const c = new THREE.Vector3();
  const ab = new THREE.Vector3();
  const ac = new THREE.Vector3();
  const normal = new THREE.Vector3();
  const edgeLengths: number[] = [];
  const minEdges = new Float32Array(triCount);
  for (let t = 0; t < triCount; t++) {
    const ia = indices[t * 3] * 3;
    const ib = indices[t * 3 + 1] * 3;
    const ic = indices[t * 3 + 2] * 3;
    a.set(positions[ia], positions[ia + 1], positions[ia + 2]);
    b.set(positions[ib], positions[ib + 1], positions[ib + 2]);
    c.set(positions[ic], positions[ic + 1], positions[ic + 2]);
    ab.subVectors(b, a);
    ac.subVectors(c, a);
    normal.crossVectors(ab, ac).normalize();
    const minEdge = Math.min(ab.length(), ac.length(), b.distanceTo(c));
    minEdges[t] = minEdge;
    edgeLengths.push(minEdge);
  }
  const sorted = [...edgeLengths].sort((x, y) => x - y);
  const thinCut = sorted.length
    ? sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.1))]
    : 0;
  const overhangCut = -0.7071; // 45 degrees below the build plane (+Z)
  for (let t = 0; t < triCount; t++) {
    const ia = indices[t * 3] * 3;
    const ib = indices[t * 3 + 1] * 3;
    const ic = indices[t * 3 + 2] * 3;
    a.set(positions[ia], positions[ia + 1], positions[ia + 2]);
    b.set(positions[ib], positions[ib + 1], positions[ib + 2]);
    c.set(positions[ic], positions[ic + 1], positions[ic + 2]);
    ab.subVectors(b, a);
    ac.subVectors(c, a);
    normal.crossVectors(ab, ac).normalize();
    const overhang = normal.z < overhangCut;
    const thin = minEdges[t] <= thinCut;
    const color = (overlay === 'overhang' && overhang)
      || (overlay === 'issues' && (overhang || thin))
      ? OVERHANG
      : (overlay === 'thin_walls' && thin) ? THIN : BASE;
    for (let k = 0; k < 3; k++) {
      colors[t * 9 + k * 3] = color.r;
      colors[t * 9 + k * 3 + 1] = color.g;
      colors[t * 9 + k * 3 + 2] = color.b;
    }
  }
  return colors;
}

export default function PartViewer({
  geometry, view, compare, resetToken = 0, onUserCamera, className = '',
}: Props) {
  const host = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const meshRef = useRef<THREE.Object3D | null>(null);
  const ghostRef = useRef<THREE.Object3D | null>(null);
  const boundsRef = useRef<{ center: THREE.Vector3; radius: number } | null>(null);
  const initialised = useRef(false);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(2, globalThis.devicePixelRatio || 1));
    renderer.setSize(el.clientWidth || 320, el.clientHeight || 280, false);
    renderer.domElement.style.width = '100%';
    renderer.domElement.style.height = '100%';
    renderer.domElement.style.display = 'block';
    el.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(
      42, (el.clientWidth || 320) / (el.clientHeight || 280), 0.01, 10_000);
    camera.up.set(0, 0, 1);
    camera.position.set(30, -30, 22);
    scene.add(new THREE.AmbientLight(0xffffff, 1.7));
    const key = new THREE.DirectionalLight(0xffffff, 2.4);
    key.position.set(1, -1.3, 2);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0x8899ff, 1.1);
    fill.position.set(-1.5, 1, -0.5);
    scene.add(fill);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.screenSpacePanning = true;
    controls.mouseButtons = {
      LEFT: THREE.MOUSE.ROTATE,
      MIDDLE: THREE.MOUSE.DOLLY,
      RIGHT: THREE.MOUSE.PAN,
    };
    controls.addEventListener('start', () => onUserCamera?.());
    const onResize = () => {
      if (!host.current) return;
      const w = host.current.clientWidth || 320;
      const h = host.current.clientHeight || 280;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };
    const ro = new ResizeObserver(onResize);
    ro.observe(el);
    let raf = 0;
    const tick = () => {
      controls.update();
      renderer.render(scene, camera);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    rendererRef.current = renderer;
    sceneRef.current = scene;
    cameraRef.current = camera;
    controlsRef.current = controls;
    initialised.current = true;
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      controls.dispose();
      renderer.dispose();
      renderer.domElement.remove();
      scene.clear();
      rendererRef.current = null;
      sceneRef.current = null;
      cameraRef.current = null;
      controlsRef.current = null;
      initialised.current = false;
    };
    // onUserCamera is a stable callback in the view; the renderer itself is
    // created once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene || !initialised.current) return;
    if (meshRef.current) {
      scene.remove(meshRef.current);
      const old = meshRef.current as THREE.Mesh;
      old.geometry?.dispose();
      const material = old.material;
      if (Array.isArray(material)) material.forEach((m) => m.dispose());
      else material?.dispose();
      meshRef.current = null;
    }
    if (ghostRef.current) {
      scene.remove(ghostRef.current);
      const old = ghostRef.current as THREE.Mesh;
      old.geometry?.dispose();
      if (Array.isArray(old.material)) old.material.forEach((m) => m.dispose());
      else old.material?.dispose();
      ghostRef.current = null;
    }
    if (!geometry || !geometry.indices?.length) {
      boundsRef.current = null;
      return;
    }
    const positions = geometry.positions;
    const indices = geometry.indices;
    const bounds = centerAndSize(positions);
    boundsRef.current = bounds;
    const base = new THREE.BufferGeometry();
    base.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    base.setIndex(indices);
    let geo: THREE.BufferGeometry = base;
    let material: THREE.Material;
    const wireframe = view?.mode === 'wireframe';
    const colors = wireframe ? null : faceOverlayColors(
      positions, indices, view?.overlay || 'none');
    if (colors) {
      geo = base.toNonIndexed();
      base.dispose();
      geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    }
    if (wireframe) {
      material = new THREE.MeshBasicMaterial({
        color: 0x8b7cf6, wireframe: true, transparent: true, opacity: 0.62,
        side: THREE.DoubleSide,
      });
    } else {
      material = new THREE.MeshStandardMaterial({
        color: colors ? 0xffffff : 0x8b93a7,
        vertexColors: !!colors,
        metalness: 0.12,
        roughness: 0.58,
        flatShading: false,
        side: THREE.DoubleSide,
      });
    }
    const mesh = new THREE.Mesh(geo, material);
    if (!geo.boundingSphere) geo.computeBoundingSphere();
    scene.add(mesh);
    meshRef.current = mesh;

    if (compare?.positions?.length && compare.indices?.length) {
      const ghostGeo = new THREE.BufferGeometry();
      ghostGeo.setAttribute('position',
        new THREE.Float32BufferAttribute(compare.positions, 3));
      ghostGeo.setIndex(compare.indices);
      const ghostMat = new THREE.MeshStandardMaterial({
        color: GHOST, transparent: true, opacity: 0.18, roughness: 0.7,
        metalness: 0, side: THREE.DoubleSide, depthWrite: false,
      });
      const ghost = new THREE.Mesh(ghostGeo, ghostMat);
      ghost.renderOrder = -1;
      scene.add(ghost);
      ghostRef.current = ghost;
    }
    return () => {
      geo.dispose();
      material.dispose();
      if (ghostRef.current) {
        scene.remove(ghostRef.current);
        const old = ghostRef.current as THREE.Mesh;
        old.geometry?.dispose();
        if (Array.isArray(old.material)) old.material.forEach((m) => m.dispose());
        else old.material?.dispose();
        ghostRef.current = null;
      }
    };
  }, [geometry, view?.mode, view?.overlay, compare]);

  // Apply a camera preset without rebuilding the mesh. `resetToken` is the
  // explicit "fit" control, so a user's orbit is never yanked away by an
  // unrelated re-render.
  useEffect(() => {
    const camera = cameraRef.current;
    const controls = controlsRef.current;
    const bounds = boundsRef.current;
    if (!camera || !controls || !bounds) return;
    const preset = view?.preset || 'iso';
    camera.position.copy(cameraPosition(preset, bounds.center, bounds.radius));
    controls.target.copy(bounds.center);
    camera.lookAt(bounds.center);
    controls.update();
  }, [view?.preset, resetToken, geometry]);

  return <div ref={host} className={`relative overflow-hidden ${className}`} />;
}
