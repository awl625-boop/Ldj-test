# LDJ $1 handbag watcher

Watches every listing on ldj.com and pushes a notification to your phone the
moment a $1 giveaway shows up in a product description.

## How it works

Shopify stores (ldj.com is one) publish a public JSON feed of every product
at /products.json, including the full description and a last-updated
timestamp. Every 5 minutes this script:

1. Pulls the whole catalog (a few requests, not 3000+ page loads)
2. Checks which listings changed since the last run
3. Scans only the changed ones for a $1 giveaway signal (an explicit coupon
   code, or the literal price "$1" standing alone, or known giveaway
   phrasing)
4. Pushes an alert with the product link the moment it finds one
5. Logs every changed description (matched or not) to changelog.md as a
   safety net, in case the wording changes in a way the patterns miss

No browser automation, no login, no checkout probing -- just reading public
listing data, the same way your own browser would when you visit the site.

## Setup (already done if you've followed along)

1. Installed the ntfy app and subscribed to topic: ldj-watch-9dc7a477
2. Created a private GitHub repo
3. Added three files: monitor.py, .github/workflows/watch.yml, README.md

## Verify it's working

- Go to the Actions tab in your repo -- you should see "LDJ coupon
  watcher" runs appearing every 5 minutes.
- Click a run -> "Run watcher" step -> you'll see logs like
  "Fetched 3142 products" and either "No matches this run" or a
  "MATCH:" line.
- The very first run just records a baseline (it won't alert on the
  existing catalog, only on changes after that).
- Check changelog.md in the repo occasionally -- it lists every listing
  whose description changed recently, tagged [MATCH] or [no match], so you
  can spot anything the detection missed.

## Tuning

- Check frequency: change the cron line in watch.yml. 5 minutes is the
  practical floor for free, reliable GitHub Actions scheduling.
- Detection pattern: CODE_PATTERNS, STANDALONE_DOLLAR_PATTERN, and
  SOFT_SIGNAL_PATTERN in monitor.py control what counts as a match. If
  changelog.md shows a real drop that was tagged [no match], share the
  wording and the patterns can be tightened.

## Costs

$0. GitHub Actions is free for public repos, and private repos get 2,000
free minutes/month, which this uses a small fraction of. ntfy.sh is free.
