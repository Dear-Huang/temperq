export default function ResearchVisual({ kind, label }) {
  if (kind === 'lgbm') return <div className="research-visual tree-visual" aria-hidden="true">
    <span className="visual-caption">{label} <span>/ BOOSTED TREES</span></span>
    <svg viewBox="0 0 320 142" fill="none">
      <g className="tree-links"><path d="M160 25V44H83V61M160 44H237V61M83 73V91H44V111M83 91H122V111M237 73V91H198V111M237 91H276V111"/></g>
      <g className="tree-nodes"><rect x="139" y="9" width="42" height="21" rx="3"/><rect x="62" y="57" width="42" height="21" rx="3"/><rect x="216" y="57" width="42" height="21" rx="3"/></g>
      <g className="tree-leaves"><rect x="30" y="108" width="28" height="15" rx="2"/><rect x="108" y="108" width="28" height="15" rx="2"/><rect x="184" y="108" width="28" height="15" rx="2"/><rect x="262" y="108" width="28" height="15" rx="2"/></g>
      <g className="tree-node-details"><path d="M149 19H160M164 19H171M72 67H83M87 67H94M226 67H237M241 67H248"/></g>
    </svg>
    <span className="visual-footer">LightGBM <span>ENSEMBLE / RANKING</span></span>
  </div>;
  if (kind === 'kronos') return <div className="research-visual sequence-visual" aria-hidden="true">
    <span className="visual-caption">{label} <span>/ TIME-SERIES MODEL</span></span>
    <div className="token-sequence">{[30,49,43,66,57,75,62,46,60,81,70,89].map((height,i)=><span className={i>8?'future-token':''} key={i} style={{'--token-height':`${height}px`}}><i/></span>)}</div>
    <div className="sequence-axis"><span>CONTEXT WINDOW</span><span>PREDICTION</span></div>
    <span className="visual-footer">Kronos <span>TOKENIZE / FINE-TUNE</span></span>
  </div>;
  return <div className="research-visual artifacts-visual" aria-hidden="true">
    <span className="visual-caption">{label} <span>/ RESEARCH ARTIFACTS</span></span>
    <div className="artifact-files"><div><span className="file-extension">YML</span><span>训练配置<small>recipe</small></span><i/></div><div><span className="file-extension">MD</span><span>评估报告<small>evaluation</small></span><i/></div><div><span className="file-extension">REV</span><span>模型版本<small>revision</small></span><i/></div></div>
    <span className="visual-footer">Research artifacts <span>BUILD / SHARE</span></span>
  </div>;
}
