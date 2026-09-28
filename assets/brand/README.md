# tlgr brand assets

`logo-512.png`, `logo-240.png` and `logo-64.png` are the logo: a geometric "t" with a block cursor, on a blurred texture in Telegram's blues.

`explorations/` keeps the directions that were tried along the way.

## Regenerating

The logo is drawn by `src/logo.html` (`bg.js` paints the texture, `mark.js` holds the mark). To render it:

```bash
cd assets/brand/src
python3 -m http.server 8765 &
./render.sh logo.html ../logo-512.png 512 512
```

`render.sh` needs Google Chrome installed; set `PORT` if 8765 is taken.
