# RAW Extension Reference

Canonical list of RAW file extensions by manufacturer.
Used during scan phase to classify `file_type = RAW`.

## Sony
- `.ARW` — Alpha series (A7, A9, A1, ZV, RX)
- `.SRF` — older DSC series
- `.SR2` — older DSC series

## Canon
- `.CR3` — mirrorless (R series) and recent DSLRs
- `.CR2` — DSLRs (5D, 7D, 80D era)
- `.CRW` — very early Canon digital

## Nikon
- `.NEF` — all Nikon DSLRs and mirrorless (Z series)
- `.NRW` — Nikon Coolpix compacts

## Fujifilm
- `.RAF` — all X-series and GFX

## Panasonic / Leica
- `.RW2` — Panasonic Lumix
- `.RWL` — Leica (Panasonic-derived)

## Olympus / OM System
- `.ORF` — all Olympus / OM System

## Pentax / Ricoh
- `.PEF` — Pentax DSLRs
- `.DNG` — some Ricoh/Pentax use Adobe DNG natively

## Samsung
- `.SRW` — NX series

## Sigma
- `.X3F` — Foveon sensor cameras

## Phase One / Mamiya
- `.IIQ` — Phase One backs

## Hasselblad
- `.3FR`, `.FFF` — H and X series

## Adobe DNG (generic)
- `.DNG` — used by many manufacturers (Leica, Ricoh, some smartphones)
  Note: DNG can also be a converted/archived RAW — check EXIF OriginalRawFileName

## Usage in code

```python
RAW_EXTENSIONS = {
    # Sony
    '.arw', '.srf', '.sr2',
    # Canon
    '.cr3', '.cr2', '.crw',
    # Nikon
    '.nef', '.nrw',
    # Fujifilm
    '.raf',
    # Panasonic/Leica
    '.rw2', '.rwl',
    # Olympus
    '.orf',
    # Pentax/Ricoh
    '.pef', '.dng',
    # Samsung
    '.srw',
    # Sigma
    '.x3f',
    # Phase One
    '.iiq',
    # Hasselblad
    '.3fr', '.fff',
}

def is_raw(path: Path) -> bool:
    return path.suffix.lower() in RAW_EXTENSIONS
```

## DNG ambiguity

`.DNG` can be:
1. A native RAW from a DNG-native camera (Leica, some Ricoh) → treat as RAW
2. A converted DNG made in Lightroom/Capture One from another RAW → treat as RAW
3. A DNG created from a JPEG (rare but possible) → treat as CAMERA_JPEG

Resolution: Check `EXIF:OriginalRawFileName` — if present, it's a converted DNG.
Either way, classify as RAW unless there's strong evidence otherwise.
