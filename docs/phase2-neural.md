# Phase 2: neural audio fingerprinting (documented, not yet built)

The WXDU library is ~3 TB / ~300-400k tracks. The Phase 1 landmark matcher
(pair/quad spectral hashing) is deliberately index-shaped so a neural
matcher can sit behind the same GraphQL schema. This file records why a
neural matcher is the long-term answer, what it will look like, and what
must happen first. Status: **planned, on a separate branch, needs corpus
access** (`feature/neural-fingerprint`).

## Why

- Row-count math: at ~5-10k rows/track the landmark table is 1.5-4B rows at
  full library size - loadable in partitioned Postgres but painful to
  rebuild, and the phase-1 benchmark produces the real density to project.
- The Shazam study motivates the upgrade: their failure modes (vinyl,
  pitch-shifted mixes, reverb/drones, degraded audio) are exactly what
  contrastive augmentation simulates.
- Storage: ~1-2 s embedding segments at 128 dims over 400k tracks is ~100-
  200M vectors; a compressed Faiss/pgvector index (5-15 GB) beats any SQL
  hash table at that scale, and recall degrades gracefully under noise.

## Basis

Chang, Lee, Park, Lim, Lee, Ko, Han. "Neural Audio Fingerprint for
High-specific Audio Retrieval based on Contrastive Learning." ICASSP 2021.
arXiv:2010.11910. MIT-licensed official code + released datasets
(mimbres/neural-audio-fp; 11 GB mini / 443 GB full).

Key properties:

- Self-supervised: positives are two random offsets of the same track plus
  degradations (noise, room/mic impulse responses, time offset jitter);
  negatives are everything else. No labels needed - the WXDU library is the
  training data.
- Trained network (~few dozen MB) embeds short segments; inference is
  fast enough for live stream recognition on modest hardware.
- Matching = maximum inner product search over the segment embedding table
  with a fine-grained verification step.

## Plan

1. **Corpus access (blocking, separate effort)**: get the ~3 TB WXDU library
   (or a several-thousand-track subset with the 42 test tracks held out as
   eval) onto a GPU box. Track this as its own task list; the station
   library lives on beachyhead, so NFS/rsync staging or an on-site GPU
   machine decides the shape. Recording enough _negative_ variety (radio
   chatter, PSAs, field recordings) is part of the corpus spec.
2. **Port the model**: the released code is TensorFlow 2.2-2.5 (2021).
   Options: (a) pfann (stdio2016/pfann) PyTorch reimplementation - small,
   unlicensed, low maintenance; (b) fresh PyTorch port of the ICASSP recipe
   (~300 lines: encoder + contrastive loss + augmentations); (c) run the
   released TF code as-is worst case. Prefer (b); verify embedding dims and
   segment length from the released checkpoints first.
3. **Training**: bootstrap with the 11 GB mini dataset to validate the
   pipeline, then train on WXDU audio. 11 GB+ VRAM GPU, batch 320-1280,
   days not weeks. Hold out (a) the 42 test tracks, (b) WXDU stream clips
   with playlist ground truth, (c) degraded variants - as the eval sets.
4. **Index**: benchmark Faiss (HNSW, PQ/quantized) vs pgvector on the same
   vectors before choosing; target recall@10 >= 95% vs exact search at
   p99 < 50 ms; the "switch at 50-100M vectors" threshold is a starting
   point, not gospel (per the gpt-6.1-sol design review).
5. **Integration**: new store adapter implementing the same interface
   (embedding write + ANN search + verify), new GraphQL field
   `identifyMatch` unchanged on the wire, A/B both matchers on the same
   clips, keep landmark matcher as fallback while confidence is compared.
6. **Ops**: the fingerprint service must rate-limit ingest and run ANN
   index builds in maintenance windows so the DJ webUI never starves
   (consultant review, beachyhead capacity notes).

## Open TODOs

- [ ] Pitch robustness in the landmark matcher: match quad hashes
      tolerantly (each 6-bit ratio within +/-2 steps; the
      `find_matches_tolerant` store query was built for this). Measured
      0/15 on +-3..6% shifts - pair and quad exact equality both break
      under the resampler chain, and the pitch-rescue pass only clears
      verification on tone-test material.
- [ ] Corpus staging plan + access (blocking; separate project/branch)
- [ ] Reproduce ICASSP numbers on the mini dataset with a modern PyTorch port
- [ ] Eval-set definition from test_tracks + WXDU playlist ground truth
- [ ] Faiss vs pgvector benchmark harness on held-out vectors
- [ ] Embedding store adapter + GraphQL A/B field
- [ ] Live-stream mode: recognize every ~20 s against both matchers, log
      agreement (reuse recognizer.py cadence)
- [ ] Score-orientation: verification alignment could also disambiguate
      near-offset ties (the vote-gap gate was removed once verification
      became the gate)
