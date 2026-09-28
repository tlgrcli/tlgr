# tlgr brand assets

`logo-1024.png` is the master; `logo-512.png`, `logo-240.png` and `logo-64.png` are downscaled from it. The logo is: a geometric "t" with a block cursor, on a blurred texture in Telegram's blues.

`explorations/` keeps the directions that were tried along the way.

## Regenerating

The logo is drawn by `src/logo.html` (`bg.js` paints the texture, `mark.js` holds the mark). To render it:

```bash
cd assets/brand/src
python3 -m http.server 8765 &
./render.sh logo.html "$PWD/../logo-1024.png" 512 512 2
```

The last argument is the device scale factor, so this renders at 1024px. Resize the master for the smaller files. `render.sh` needs Google Chrome installed; set `PORT` if 8765 is taken.

The mark is vector outlines with real rounded corners (`roundedPath` in `mark.js`), and only the background canvas is blurred, so the "t" stays sharp at every size.
