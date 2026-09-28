// Flowing strand texture, deterministic.
function mulberry(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}
function drawWaves(canvas, opts={}){
  const W=canvas.width, H=canvas.height, ctx=canvas.getContext('2d');
  const rnd=mulberry(opts.seed||7);
  const s=Math.min(W,H)/480;
  const g=ctx.createLinearGradient(0,0,W,H);
  g.addColorStop(0,opts.bg0||'#0b3a5c');g.addColorStop(1,opts.bg1||'#0f4a73');
  ctx.fillStyle=g;ctx.fillRect(0,0,W,H);
  const pal=opts.pal||[[14,72,112],[23,120,176],[34,158,217],[42,171,238],[150,214,247],[232,246,254]];
  const N=opts.n||1400, amp=(opts.amp||1)*s;
  const f=(opts.freq||1)/ (Math.max(W,H)/480);
  for(let i=0;i<N;i++){
    const t=i/N;
    const y0=-H*0.35+t*H*1.7;
    // band selection: sand bands appear in two flowing ribbons
    const band=Math.sin(t*Math.PI*(opts.bands||3.2)+ (opts.phase||0.6));
    let c;
    if(band>0.82) c=pal[4+ (rnd()<0.5?0:1)];
    else if(band>0.55) c=pal[3];
    else if(band>0.1) c=pal[2];
    else if(band>-0.5) c=pal[1];
    else c=pal[0];
    const shade=(opts.shade0??0.7)+(1-(opts.shade0??0.7))*Math.sin(t*Math.PI*9+1.3);
    ctx.strokeStyle=`rgba(${c[0]*shade|0},${c[1]*shade|0},${c[2]*shade|0},${(opts.alpha??0.34)+rnd()*0.3})`;
    ctx.lineWidth=(0.5+rnd()*0.8)*s;
    ctx.beginPath();
    for(let x=-40*s;x<=W+40*s;x+=4*s){
      const u=x*f;
      const y=y0 + 70*amp*Math.sin(u*0.011+t*5.2+ (opts.twist||0))
                 + 34*amp*Math.sin(u*0.023 - t*9.1 + 1.7)
                 + 12*amp*Math.sin(u*0.05 + t*21)
                 - x*(opts.slope||0.35)*(H/W);
      x<=-40*s?ctx.moveTo(x,y):ctx.lineTo(x,y);
    }
    ctx.stroke();
  }
  // vignette
  const v=ctx.createRadialGradient(W*0.5,H*0.5,Math.min(W,H)*0.2,W*0.5,H*0.5,Math.max(W,H)*0.75);
  v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,`rgba(6,34,56,${opts.vignette??0.55})`);
  ctx.fillStyle=v;ctx.fillRect(0,0,W,H);
}
