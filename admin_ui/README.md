# Admin UI

## KB scraper embed

The `Web Crawling` and `Content Manager` routes now embed the standalone KB scraper frontend.

Configure scraper URL with:

```bash
VITE_KB_SCRAPER_URL=/kb-scraper
```

If unset, it defaults to `/kb-scraper` (same host/port as main app).
