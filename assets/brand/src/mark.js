// Mark: geometric "t" with an angled terminal and a block cursor, as vector outlines
// with true rounded corners (no filters, so the edges stay sharp at any size).
function roundedPath(pts){
  // pts: [x, y, r]; r is the corner radius at that vertex (0 keeps it sharp).
  const n=pts.length; let d='';
  for(let i=0;i<n;i++){
    const [x,y,r]=pts[i], [px,py]=pts[(i-1+n)%n], [nx,ny]=pts[(i+1)%n];
    if(!r){d+=(i?'L':'M')+x.toFixed(2)+' '+y.toFixed(2);continue;}
    const l1=Math.hypot(px-x,py-y), l2=Math.hypot(nx-x,ny-y);
    const k1=Math.min(r,l1/2)/l1, k2=Math.min(r,l2/2)/l2;
    const ax=x+(px-x)*k1, ay=y+(py-y)*k1, bx=x+(nx-x)*k2, by=y+(ny-y)*k2;
    d+=(i?'L':'M')+ax.toFixed(2)+' '+ay.toFixed(2)+'Q'+x+' '+y+' '+bx.toFixed(2)+' '+by.toFixed(2);
  }
  return d+'Z';
}
function arc(cx,cy,rad,a0,a1,steps=40){
  const out=[];for(let i=1;i<steps;i++){const a=(a0+(a1-a0)*i/steps)*Math.PI/180;out.push([cx+rad*Math.cos(a),cy+rad*Math.sin(a),0]);}return out;
}
const R=8, C=5; // convex and concave radii, in the 200-unit box
const T=[[62,40,R],[94,22,R],[94,64,C],[140,64,R],[140,94,R],[94,94,C],[94,122,0],
  ...arc(132,122,38,180,90),[132,160,0],[140,160,R],[140,190,R],[132,190,0],
  ...arc(132,120,70,90,180),[62,120,0],[62,94,C],[36,94,R],[36,64,R],[62,64,C]];
const CURSOR=[[154,112,R],[182,112,R],[182,190,R],[154,190,R]];
const MARK='<svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg" shape-rendering="geometricPrecision"><g fill="#fff" transform="translate(-9 -6)"><path d="'+roundedPath(T)+'"/><path d="'+roundedPath(CURSOR)+'"/></g></svg>';
