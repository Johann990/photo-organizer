# Resized JPEG Detection — Signal Reference

How to identify derivative / export / resized JPEGs that are safe to delete.
Signals are applied in order; first match wins for classification.

## Confirmed Rule (from user)

**Path contains `resized` (case-insensitive)**
```python
if "resized" in str(path).lower():
    return FileType.RESIZED_JPEG
```
This was explicitly confirmed by the user. Apply unconditionally.

---

## Secondary Signals (verify against first scan report before enabling)

These are common patterns that may or may not apply to this specific library.
Review the Scan Report (Phase 2) to confirm which apply before enabling.

### 1. Folder name patterns
Common export/share folder names:
```python
RESIZED_FOLDER_SIGNALS = {
    'resized', 'resize',
    'export', 'exports',
    'share', 'shared', 'sharing',
    'web', 'websize', 'web_size',
    'small', 'thumb', 'thumbs', 'thumbnails',
    'preview', 'previews',
    'instagram', 'ig',
    'blog',
    'send', 'sent',
    'proof', 'proofs',
}

def folder_signal(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return bool(parts & RESIZED_FOLDER_SIGNALS)
```

### 2. Filename suffix patterns
```python
RESIZED_SUFFIX_PATTERNS = [
    r'_sm$', r'_small$',
    r'_web$', r'_w$',
    r'_thumb$', r'_tn$',
    r'_\d{3,4}px$',        # e.g. _1200px
    r'-resized$',
    r'_export$',
    r'_share$',
    r'_ig$',               # Instagram
    r'_blog$',
]

import re
def suffix_signal(stem: str) -> bool:
    stem_lower = stem.lower()
    return any(re.search(pat, stem_lower) for pat in RESIZED_SUFFIX_PATTERNS)
```

### 3. Resolution below camera native threshold
```python
# Determine per-camera-model what "native" resolution is after scan
# Example: Sony A7R II → 7952 × 5304; anything < 4000px wide is suspect
RESIZED_WIDTH_THRESHOLD = 2500  # conservative default; tune after scan report

def resolution_signal(width: int, height: int) -> bool:
    return width > 0 and width < RESIZED_WIDTH_THRESHOLD
```
⚠️ Do not use this signal alone — some legitimate camera-native JPEGs from
older or compact cameras may be below this threshold.

### 4. EXIF Software field indicates export tool
```python
EXPORT_SOFTWARE_SIGNALS = [
    'lightroom', 'aperture', 'capture one', 'darktable',
    'photos', 'google photos', 'flickr',
    'squarespace', 'wordpress',
    'whatsapp', 'line', 'telegram',
]

def software_signal(software: str) -> bool:
    if not software:
        return False
    sw = software.lower()
    return any(s in sw for s in EXPORT_SOFTWARE_SIGNALS)
```
⚠️ Many edited-but-full-resolution exports will also match this. Use as a
supporting signal, not a standalone reason to classify as RESIZED.

### 5. File size anomaly
If a JPEG file is unusually small relative to its resolution, it may be
a heavily compressed export:
```python
def filesize_signal(size_bytes: int, width: int, height: int) -> bool:
    if width == 0 or height == 0:
        return False
    bytes_per_pixel = size_bytes / (width * height)
    return bytes_per_pixel < 0.3   # very rough heuristic; tune empirically
```

---

## Classification Logic

```python
def classify_jpeg(path: Path, exif: dict, size_bytes: int) -> FileType:
    # Rule 1: confirmed — path contains "resized"
    if "resized" in str(path).lower():
        return FileType.RESIZED_JPEG

    # Rules 2+: only after user confirms in Phase 2 review
    signals = 0
    if folder_signal(path):       signals += 2   # strong signal
    if suffix_signal(path.stem):  signals += 2   # strong signal
    if resolution_signal(exif.get("ImageWidth", 0),
                         exif.get("ImageHeight", 0)):
        signals += 1                              # weak signal
    if software_signal(exif.get("Software", "")):
        signals += 1                              # weak signal

    # Require ≥ 2 signal points to auto-classify (avoids false positives)
    if signals >= 2:
        return FileType.RESIZED_JPEG

    return FileType.CAMERA_JPEG
```

---

## Handling Ambiguous Cases

Files that score 1 signal point (weak): classify as `UNKNOWN`, surface in
Scan Report for user review. Never auto-delete UNKNOWN files.

The goal is **zero false positives** — it is much better to keep a resized
JPEG than to delete a camera-native one.
