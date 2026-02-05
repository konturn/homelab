# nkontur.com

Modern, minimalist personal website for Noah Kontur.

## Files

```
website/
├── index.html        # Homepage with hero, background, quick links
├── publications.html # NASA research and technical publications
├── poetry.html       # Poetry collection
├── contact.html      # Contact info and availability
├── style.css         # Shared styles (dark theme, responsive)
├── net-map/
│   └── index.html    # Network map placeholder
└── README.md         # This file
```

## Design

- **Theme:** Minimalist dark (near-black background, subtle borders)
- **Typography:** Inter font with system fallbacks
- **Layout:** Max-width 720px, centered, generous whitespace
- **Responsive:** Mobile-first, works on all screen sizes
- **Animations:** Subtle fade-in on page load, hover transitions

## Deployment

Copy all files to the nginx webroot:

```bash
# From router or via rsync
cp -r /path/to/website/* /data/webroot/html/
```

For nginx to serve the HTML files without extensions:

```nginx
# In nginx.conf or site config
location / {
    try_files $uri $uri/ $uri.html =404;
}
```

## Local Preview

```bash
cd /home/node/.openclaw/workspace/website
python3 -m http.server 8080
# Open http://localhost:8080
```

## Customization

CSS variables in `style.css` control theming:

```css
:root {
  --bg-primary: #0a0a0b;
  --accent: #3b82f6;
  /* etc. */
}
```

## Notes

- No build step required — plain HTML/CSS
- Google Fonts (Inter) loaded via CDN
- Inline SVG icons for contact page
- Poetry section uses `white-space: pre-line` for verse formatting
