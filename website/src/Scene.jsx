import { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { Pause, Play } from 'lucide-react';

export default function Scene() {
  const host = useRef(null), running = useRef(true);
  const [paused,setPaused] = useState(false), [failed,setFailed] = useState(false);
  useEffect(()=>{
    const container=host.current;
    let renderer, frame, observer, envTarget, pmrem;
    const geometry=[], materials=[];
    try {
      renderer=new THREE.WebGLRenderer({antialias:true,alpha:true,preserveDrawingBuffer:true});
      renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));
      renderer.setClearColor(0xffffff,0);
      renderer.toneMapping=THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure=.8;
      container.appendChild(renderer.domElement);
      const scene=new THREE.Scene();
      const camera=new THREE.PerspectiveCamera(32,1,.1,80);
      camera.position.set(0,1.4,15);
      camera.lookAt(0,0,0);
      const environment=new THREE.Scene();
      environment.background=new THREE.Color('#67758a');
      const panelGeo=new THREE.PlaneGeometry(16,16);geometry.push(panelGeo);
      for(const [color,intensity,position]of [['#ffffff',5,[-6,7,5]],['#ffffff',3,[6,4,0]],['#2865ff',2,[-7,-3,-2]],['#c9fcf1',2,[8,-2,3]]]) {
        const material=new THREE.MeshBasicMaterial({color,side:THREE.DoubleSide});
        material.color.multiplyScalar(intensity);materials.push(material);
        const panel=new THREE.Mesh(panelGeo,material);panel.position.set(...position);panel.lookAt(0,0,0);environment.add(panel);
      }
      pmrem=new THREE.PMREMGenerator(renderer);envTarget=pmrem.fromScene(environment,.025);scene.environment=envTarget.texture;
      scene.add(new THREE.HemisphereLight('#ffffff','#8e9cb1',1.2));
      const key=new THREE.DirectionalLight('#ffffff',2.1);key.position.set(-4,8,6);scene.add(key);
      const fill=new THREE.DirectionalLight('#8fcaff',1.2);fill.position.set(5,-3,3);scene.add(fill);
      // Sequential glass slices express a learned signal surface.
      const group=new THREE.Group();scene.add(group);
      const sliceGeo=new RoundedBoxGeometry(.13,2.65,1.32,4,.065);geometry.push(sliceGeo);
      const edgeGeo=new RoundedBoxGeometry(.022,2.57,.022,2,.011);geometry.push(edgeGeo);
      const palette=['#2145e7','#197bf2','#16bdaa','#92d99b','#d8e450','#ff794f'].map(c=>new THREE.Color(c));
      const slices=[];
      for(let i=0;i<43;i++) {
        const t=i/42,blend=t*(palette.length-1),k=Math.min(palette.length-2,Math.floor(blend));
        const color=palette[k].clone().lerp(palette[k+1],blend-k);
        const material=new THREE.MeshPhysicalMaterial({color,metalness:.12,roughness:.09,transmission:.48,thickness:1.65,ior:1.48,clearcoat:1,iridescence:.2,iridescenceIOR:1.3,attenuationDistance:1.8,attenuationColor:color});materials.push(material);
        const slice=new THREE.Group();slice.add(new THREE.Mesh(sliceGeo,material));
        const edgeMat=new THREE.MeshStandardMaterial({color:color.clone().multiplyScalar(.83),metalness:.55,roughness:.19});materials.push(edgeMat);
        const edge=new THREE.Mesh(edgeGeo,edgeMat);edge.position.z=.666;slice.add(edge);
        slice.position.x=(t-.5)*7.3;slice.position.y=Math.sin(t*Math.PI*2-.7)*.48;slice.position.z=Math.cos(t*Math.PI*2)*.36;
        slice.rotation.x=Math.sin(t*Math.PI*1.5-.5)*.95;slice.rotation.z=Math.cos(t*Math.PI*2)*.16;
        group.add(slice);slices.push(slice);
      }
      group.rotation.set(-.15,-.34,-.12);
      const lineMat=new THREE.LineBasicMaterial({color:'#b7c3d4',transparent:true,opacity:.3});materials.push(lineMat);
      const linePoints=Array.from({length:101},(_,i)=>{const t=i/100;return new THREE.Vector3((t-.5)*8.2,-1.95+Math.sin(t*Math.PI*2)*.1,0);});
      const lineGeo=new THREE.BufferGeometry().setFromPoints(linePoints);geometry.push(lineGeo);scene.add(new THREE.Line(lineGeo,lineMat));
      let pointer={x:0,y:0},elapsed=0,last=0,visible=true;
      const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;running.current=!reduced;setPaused(reduced);
      const move=e=>{const r=container.getBoundingClientRect();pointer={x:(e.clientX-r.left)/r.width-.5,y:(e.clientY-r.top)/r.height-.5};};
      container.addEventListener('pointermove',move);
      const visibility=new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;},{rootMargin:'100px'});visibility.observe(container);
      const resize=()=>{const r=container.getBoundingClientRect();renderer.setSize(r.width,r.height);camera.aspect=r.width/r.height;camera.position.z=innerWidth<640?17.5:12;camera.updateProjectionMatrix();};
      observer=new ResizeObserver(resize);observer.observe(container);
      const draw=now=>{const delta=Math.min((now-last)/1000||0,.04);last=now;if(running.current&&visible&&!document.hidden)elapsed+=delta;group.rotation.y=-.34+Math.sin(elapsed*.22)*.12+pointer.x*.16;group.rotation.x=-.15+pointer.y*.07;group.position.y=Math.sin(elapsed*.4)*.07;slices.forEach((slice,i)=>{slice.rotation.x=Math.sin(i/42*Math.PI*1.5-.5+elapsed*.15)*.95;});if(visible&&!document.hidden)renderer.render(scene,camera);frame=requestAnimationFrame(draw);};frame=requestAnimationFrame(draw);
      return()=>{cancelAnimationFrame(frame);observer.disconnect();visibility.disconnect();container.removeEventListener('pointermove',move);geometry.forEach(g=>g.dispose());materials.forEach(m=>m.dispose());envTarget.dispose();pmrem.dispose();renderer.dispose();renderer.domElement.remove();};
    } catch {setFailed(true);renderer?.domElement.remove();renderer?.dispose();geometry.forEach(g=>g.dispose());materials.forEach(m=>m.dispose());envTarget?.dispose();pmrem?.dispose();}
  },[]);
  return <><div className="hero-scene" ref={host} role="img" aria-label="43 层棱镜玻璃组成的动态信号曲面">{failed&&<img src="/hero-poster.png" alt="棱镜信号曲面"/>}</div>{!failed&&<button className="scene-control icon-button" title={paused?'播放动态场景':'暂停动态场景'} aria-label={paused?'播放动态场景':'暂停动态场景'} onClick={()=>{running.current=!running.current;setPaused(!running.current);}}>{paused?<Play size={14}/>:<Pause size={14}/>}</button>}</>;
}
