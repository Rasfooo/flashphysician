# FlashPhysician — Submission Links (manual)

## Awesome list PRs

### 1. ricco020/awesome-data-recovery (BEST FIT)

**Direct link** (click to open PR form):

https://github.com/ricco020/awesome-data-recovery/compare/main...Rasfooo:awesome-data-recovery:add-flashphysician

- Title: `Add FlashPhysician (USB/SD rescue tool)`
- Body: see `pr_body_awesome_data_recovery.md`
- Base branch: `main` (or `master` — check list's default)

### 2. cugu/awesome-forensics

**Setup needed**: fork + branch first (similar to #1), then:

https://github.com/cugu/awesome-forensics/compare/master...Rasfooo:awesome-forensics:add-flashphysician

- Title: `Add FlashPhysician (USB/SD rescue tool with carving)`
- Body: see `pr_body_awesome_forensics.md`
- Section: `### Carving` (after photorec entry)

### 3. bst04/tools-for-data-recovery

**Setup needed**: fork + branch first

https://github.com/bst04/tools-for-data-recovery/compare/main...Rasfooo:tools-for-data-recovery:add-flashphysician

- Title: `Add FlashPhysician`
- Body: see `pr_body_bst04.md`

---

## Social posts

### Reddit r/datarecovery

**Subreddit**: r/datarecovery
**Title**: `Open-source rescue tool for USB flash / SD card — diagnose, repair, recover (FlashPhysician)`

**Body**: see `reddit_post.md`

**Posting guidelines**:
- Disclose affiliation ("I built this")
- Don't spam — one post, engage in comments
- Best time: Tue–Thu, morning US Eastern

### Hacker News (Show HN)

**Title**: `Show HN: FlashPhysician – diagnose and repair corrupt USB/SD drives`
**URL**: https://github.com/Rasfooo/flashphysician

**First comment (post immediately)**: see `hn_comment.md`

**Timing**: Tue–Thu, 9am–12pm US Eastern

---

## PAT token scope issue

The `GITHUB_TOKEN` used for pushing does NOT have scope `public_repo`, so it
can fork repos and push to forks, but cannot create PRs against upstream repos.

**Fix options**:

1. **Manual PR**: use the direct comparison links above in a browser
2. **Regenerate PAT** with `public_repo` scope, set `GITHUB_TOKEN` in `/opt/data/.env`
3. **Use `gh auth login`** with a device code flow (interactive, gets full scopes)

---

## Checklist

- [ ] PR: ricco020/awesome-data-recovery (via link)
- [ ] PR: cugu/awesome-forensics (fork + branch + link)
- [ ] PR: bst04/tools-for-data-recovery (fork + branch + link)
- [ ] Reddit r/datarecovery post
- [ ] HN Show HN submission + first comment
- [ ] (optional) Dev.to article after traction
