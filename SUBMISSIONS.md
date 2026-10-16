# FlashPhysician — Submission Links (manual)

## ⚡ Quick action: create 3 PRs (click links below)

**Note**: The `GITHUB_TOKEN` used for pushing can fork repos and push to forks,
but lacks the `public_repo` scope needed to create PRs against upstream repos.
Below are **direct comparison links** — open in browser, GitHub prefills the PR form.

---

### 1. ⭐ ricco020/awesome-data-recovery — **BEST FIT**

**Link**: https://github.com/ricco020/awesome-data-recovery/compare/main...Rasfooo:awesome-data-recovery:add-flashphysician

- **Title**: `Add FlashPhysician (USB/SD rescue tool)`
- **Body**: copy from `/opt/data/cache/scratch/pr_body_awesome_data_recovery.md`
- **Base**: `main` (or `master` — check default)
- **Section**: `## Free & open-source` (after Safecopy)
- **Status**: ✅ fork + branch + push done

---

### 2. cugu/awesome-forensics

**Link**: https://github.com/cugu/awesome-forensics/compare/master...Rasfooo:awesome-forensics:add-flashphysician

- **Title**: `Add FlashPhysician (USB/SD rescue tool with carving)`
- **Body**: copy from `/opt/data/cache/scratch/pr_body_awesome_forensics.md`
- **Base**: `master`
- **Section**: `### Carving` (after photorec, line 135)
- **Status**: ✅ fork + branch + push done

---

### 3. bst04/tools-for-data-recovery

**Link**: https://github.com/bst04/tools-for-data-recovery/compare/main...Rasfooo:tools-for-data-recovery:add-flashphysician

- **Title**: `Add FlashPhysician (free, open-source USB/SD rescue)`
- **Body**: copy from `/opt/data/cache/scratch/pr_body_bst04.md`
- **Base**: `main`
- **Section**: table row after TestDisk (line 16)
- **Status**: ✅ fork + branch + push done

---

## Social posts

### Reddit r/datarecovery

**Subreddit**: r/datarecovery
**Title**: `Open-source rescue tool for USB flash / SD card — diagnose, repair, recover (FlashPhysician)`

**Body**: copy from `/opt/data/cache/scratch/reddit_post.md`

**Guidelines**:
- Disclose affiliation ("I built this")
- One post, engage in comments
- Best time: Tue–Thu, morning US Eastern
- Don't spam cross-posts

---

### Hacker News (Show HN)

**Title**: `Show HN: FlashPhysician – diagnose and repair corrupt USB/SD drives`
**URL**: https://github.com/Rasfooo/flashphysician

**First comment (post immediately after submitting)**: copy from `/opt/data/cache/scratch/hn_comment.md`

**Timing**: Tue–Thu, 9am–12pm US Eastern

---

## PAT token scope fix (optional, permanent)

The `GITHUB_TOKEN` in `/opt/data/.env` currently has scopes like `repo` but NOT
`public_repo`. To enable automated PR creation:

**Option A**: Regenerate PAT at https://github.com/settings/tokens with `public_repo` scope
**Option B**: Use `gh auth login --web` (device code flow) — interactive, gets full scopes
**Option C**: Keep using manual links (current state)

---

## Checklist

- [ ] PR: ricco020/awesome-data-recovery (priority 1 — click link)
- [ ] PR: cugu/awesome-forensics (priority 2 — click link)
- [ ] PR: bst04/tools-for-data-recovery (priority 3 — click link)
- [ ] Reddit r/datarecovery post
- [ ] HN Show HN submission + first comment
- [ ] (optional) Dev.to article after traction

**Spacing**: 1 submission per day, don't spam all at once.

---

## Files

- `pr_body_awesome_data_recovery.md` — PR 1 body
- `pr_body_awesome_forensics.md` — PR 2 body
- `pr_body_bst04.md` — PR 3 body
- `reddit_post.md` — Reddit post body
- `hn_comment.md` — HN first comment
- `MARKETING.md` (in repo) — full marketing pack with rationale
