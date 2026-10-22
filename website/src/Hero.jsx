import { lazy, Suspense } from 'react';
import { motion, useReducedMotion } from 'motion/react';
import { ArrowDown, ArrowRight, ArrowUpRight, Check, Cpu, GitBranch } from 'lucide-react';
const Scene=lazy(()=>import('./Scene'));
export default function Hero({open,scroll}) {
  const reduced = useReducedMotion();
  return <section className="hero" id="home">
    <motion.div className="hero-top" initial={reduced ? false : {opacity:0,y:18}} animate={{opacity:1,y:0}} transition={{duration:.8,ease:[.22,1,.36,1]}}>
      <a className="release-pill" href="/community"><span className="live-dot"/><span>独立研究，开放生长</span><span className="pill-divider"/><span>探索研究社区</span><ArrowUpRight size={13}/></a>
      <h1>Quant Workbench<span>把你的想法，训练成模型。</span></h1>
      <p className="hero-description">端到端量化机器学习平台。<br className="mobile-break"/> 本地训练，严谨验证，开放分享。</p>
      <div className="hero-actions"><button className="button button-dark button-large" onClick={()=>open('start')}>开始训练 <ArrowUpRight size={17}/></button><a className="hero-community-link" href="/community">进入研究社区 <ArrowRight size={16}/></a></div>
    </motion.div>
    <Suspense fallback={<div className="scene-loading"/>}><Scene/></Suspense>
    <div className="scene-caption caption-left"><span className="caption-index">01 /</span><div><span>IDEA → MODEL</span><strong>你的数据。你的算力。你的模型。</strong></div></div>
    <div className="scene-caption caption-right"><span className="caption-index">02 /</span><div><span>MODEL → EVIDENCE</span><strong>每一个结果，都有方法可循。</strong></div></div>
    <div className="hero-bottom"><div className="hero-assurance"><span><Cpu size={13}/> 本地优先</span><span><Check size={13}/> 可复现训练</span><span><GitBranch size={13}/> 开放研究</span></div><button onClick={()=>scroll('ecosystem')} aria-label="向下探索"><ArrowDown size={17}/></button><span className="hero-edition">QUANT RESEARCH, IN YOUR HANDS.</span></div>
  </section>;
}
