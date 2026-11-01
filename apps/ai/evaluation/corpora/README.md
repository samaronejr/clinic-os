# Engineering corpus v1

The twelve scripts are self-authored, synthetic and dedicated to the public
domain under CC0-1.0. No patient audio, PHI, external dataset or licensed clinical
reference was used. `audio-v1.json` contains 118,927 decoded bytes of static
mono Ogg/Opus speech, and is 162,849 bytes including base64 and metadata.
Every clip has an SHA-256 and a binding to its script's exact UTF-8 SHA-256.
The corpus identity hashes both the manifest and the entire audio registry.

Generation occurred offline on October 28, 2026 with the workstation's
`/usr/bin/ffmpeg` Flite filter and the `kal`, `slt` and `rms` voices. The generator
is `ops/testing/ai_evaluation_audio.py`; invoke it explicitly with
`--output <new-file>` on a host with FFmpeg/Flite, inspect the result and register
a new corpus version rather than replacing a previously scored corpus.
Encoder/container bytes may change on another toolchain; the recorded bytes,
not a regeneration, are the evaluated artifact.

The noise case adds deterministic low-volume white noise. The interruption
case silences a bounded interval. The microphone-failure clip is silent.
Device and accent tags describe engineering simulations and synthetic voices,
not validated physical devices, pt-BR accents or representative populations.
The family, conflict, decimal, abbreviation, wrong-identity, stale-authority,
malicious-content and negation cases are scripted challenges.

Application code, tests and CI only decode and verify these static files.
They never execute the generator, FFmpeg or Flite. The offline adapter does
not understand or clinically transcribe these scripts. Human labels are
required for a final verdict; scripted test labels are not clinical approval.
EG-3 and EG-11 remain external, and this corpus supports no real-world
performance or safety claim.
