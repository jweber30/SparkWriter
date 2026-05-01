# SparkWriter Receipts

This document describes the current SparkWriter receipt model and the near-term direction for improving it.

If this document and the runtime disagree, trust:

- `src/spark_writer/receipts.py`
- `src/spark_writer/window.py`
- `src/spark_writer/sources/catalog.py`
- `tests/test_sources.py`
- `tests/test_receipts.py`

## Summary

SparkWriter already has the right high-level receipt shape:

- one SparkWriter-owned receipt per run
- one `source` section for upstream installer provenance
- one `sparkplugs` list for the selected customization layers
- optional final artifact hashes and device write metadata

That shape is good. The main work left is filling in more detail and testing it more thoroughly.

## Why Receipts Exist

A receipt should answer a small set of operator questions:

- which upstream installer was used
- what SparkWriter says it verified about that installer
- which SparkWriter manifests were applied
- what output artifacts SparkWriter produced
- what device SparkWriter wrote to

Receipts should stay SparkWriter-assembled. SparkPlugs can contribute data, but the top-level receipt should remain a SparkWriter-owned record of the whole run.

## Current Conceptual Model

SparkWriter now has two distinct layers:

- `Source`: the upstream installer definition
- `SparkPlug`: a customization layer applied to that source

That split is reflected in the current SparkWriter-owned receipt builder:

- `source` records the chosen installer
- `sparkplugs` records the selected manifests

This is the right trust boundary:

- Source trust: where the installer came from
- SparkPlug trust: how SparkWriter customized it

## Current Receipt Payload

Today, `src/spark_writer/receipts.py` builds a payload like this:

```json
{
  "identity": {
    "receipt_format_version": "1.0",
    "spark_writer_version": "unknown",
    "generated_at": "2026-05-01T00:00:00Z"
  },
  "source": {
    "id": "ubuntu-24.04-server",
    "name": "Ubuntu 24.04 LTS Server",
    "family": "ubuntu",
    "version": "24.04.3",
    "acquire": {
      "url": "https://example.com/ubuntu.iso",
      "kind": "torrent"
    },
    "verification": {
      "sha256": "expected-source-sha256"
    },
    "installer_scheme": "ubuntu-nocloud",
    "capabilities": ["cloud-init-nocloud"]
  },
  "sparkplugs": [
    {
      "id": "ubuntu-autoinstall",
      "name": "Ubuntu Autoinstall",
      "version": "1.0.0"
    }
  ],
  "final_artifacts": {
    "original_iso_sha256": "sha256:...",
    "processed_iso_sha256": "sha256:..."
  },
  "device_write": {
    "path": "/dev/sdb",
    "model": "SanDisk Ultra",
    "serial": "1234",
    "size": 62008590336,
    "started_at": "2026-05-01T00:00:00Z",
    "completed_at": "2026-05-01T00:05:00Z"
  }
}
```

## What Is Implemented Today

The current SparkWriter-owned receipt builder records:

- receipt format version
- SparkWriter version if supplied by the caller
- generated timestamp
- source ID, name, family, version
- source acquisition URL and optional acquisition kind
- declared source checksum
- source installer scheme and capabilities
- selected SparkPlug IDs, names, and optional versions
- original and processed ISO hashes when the files exist
- optional device path, model, serial, size, and timestamps
- optional `observed_environment`

The current builder is intentionally small and deterministic.

## What Is Tested Today

Receipt coverage exists, but it is narrow.

Current tests cover:

- deterministic canonicalization and signing helpers in `tests/test_receipts.py`
- one integration-style SparkWriter receipt check in `tests/test_sources.py`

That source-level test currently proves only that:

- `source.id` is emitted
- the SparkPlug ID is emitted
- `original_iso_sha256` appears when an ISO path is supplied

So the receipt subsystem is real, but still lightly exercised.

## What The Current Receipt Does Not Yet Capture

These fields are discussed in design conversations, but they are not broadly represented in the current SparkWriter-owned receipt payload:

- generated artifact hashes per SparkPlug
- partition/file inventory written to the USB device
- explicit secret disclosure state
- receipt sections contributed by individual SparkPlugs
- observed verification result details beyond the declared source SHA256
- distinction between declared verification policy and observed verification outcome
- privacy-preserving device fingerprinting
- canonical SparkWriter receipt signing

Those are all reasonable next steps, but they should be described as planned enhancements, not as current behavior.

## Recommended Receipt Rules

These rules fit both the current implementation and the next likely iteration.

### 1. One Receipt Per Run

SparkWriter should emit one top-level receipt for one flash or save workflow.

### 2. Source Stays Top-Level

The top-level `source` section should remain separate from `sparkplugs`.

That avoids implying that a SparkPlug owns upstream provenance.

### 3. SparkPlugs Stay Additive

Each selected SparkPlug should contribute its own identity and, later, its own artifact/change summary.

### 4. SparkWriter Assembles The Final Receipt

SparkPlugs may supply facts, but SparkWriter should own:

- timestamps
- source metadata
- device-write metadata
- whole-run artifact summaries
- final serialization

### 5. Observations Should Be Labeled Honestly

If environment data is included, it should stay under a clearly advisory section such as `observed_environment`.

SparkWriter can record what it observed. It should not overclaim what it proved.

## Near-Term Direction

The most practical next version of the SparkWriter-owned receipt would add detail without changing the top-level model:

1. keep `identity`, `source`, `sparkplugs`, `final_artifacts`, and `device_write`
2. add optional per-SparkPlug `artifacts`, `changes`, and `secrets`
3. add optional device-side write inventory for partitions/files created by SparkWriter-owned actions
4. record verification outcome fields separately from declared source policy
5. broaden tests before making the receipt format feel more official

## Example Of A Plausible Next Receipt

This is directionally useful, but not fully implemented today:

```json
{
  "identity": {
    "receipt_format_version": "1.1",
    "spark_writer_version": "unknown",
    "generated_at": "2026-05-01T00:05:00Z"
  },
  "source": {
    "id": "ubuntu-24.04-server",
    "name": "Ubuntu 24.04 LTS Server",
    "family": "ubuntu",
    "acquire": {
      "url": "https://releases.ubuntu.com/...",
      "kind": "torrent"
    },
    "verification_policy": {
      "sha256": "expected hash"
    },
    "verification_result": {
      "sha256_verified": true,
      "observed_sha256": "sha256:..."
    },
    "installer_scheme": "ubuntu-nocloud"
  },
  "sparkplugs": [
    {
      "id": "ubuntu-autoinstall",
      "name": "Ubuntu Autoinstall",
      "version": "1.0.0",
      "artifacts": {
        "user-data": "sha256:...",
        "meta-data": "sha256:..."
      },
      "changes": [
        "generated cloud-init user-data",
        "generated cloud-init meta-data"
      ],
      "secrets": {
        "embedded": false
      }
    }
  ],
  "final_artifacts": {
    "original_iso_sha256": "sha256:...",
    "processed_iso_sha256": "sha256:..."
  },
  "device_write": {
    "path": "/dev/sdb",
    "model": "SanDisk Ultra",
    "started_at": "2026-05-01T00:00:00Z",
    "completed_at": "2026-05-01T00:05:00Z"
  }
}
```

## Implementation Gaps To Track

From reviewing the current code and tests, these are the clearest gaps:

1. The SparkWriter-owned receipt builder does not yet capture per-SparkPlug artifacts, changes, or secret disclosure.
2. The SparkWriter-owned receipt builder records declared source SHA256, but not an explicit verification outcome model.
3. Device write metadata is present, but there is no richer inventory of partitions/files written by post-write actions.
4. The builder is only lightly tested at the SparkWriter layer.
5. There are now two receipt concepts in the codebase:
   - the SparkWriter-owned whole-run receipt in `src/spark_writer/receipts.py`
   - the manifest action `generate_receipt` in `src/spark_writer/plugins/json_plugin.py`

That last point is important. The code supports both:

- a SparkWriter-owned run receipt
- a manifest-authored signed receipt payload

Those are not the same artifact, and the docs should keep them distinct.

## Recommendation

For the next pass, keep the SparkWriter-owned receipt intentionally boring:

- top-level source provenance
- selected SparkPlug identities
- final ISO hashes
- device metadata

Then extend it around the Ubuntu autoinstall flow with:

- generated `user-data` and `meta-data` artifact hashes
- explicit secret disclosure semantics
- better tests around the actual emitted payload

That would give SparkWriter a receipt story that is both honest and demonstrable.
