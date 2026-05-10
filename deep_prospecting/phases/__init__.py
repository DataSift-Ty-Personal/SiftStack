"""Phase modules for the deep_prospecting pipeline.

Each phase consumes a typed input and produces a typed output (see
deep_prospecting.models). Phases are async and use the `_safe` /
`_safe_call` wrappers from `deep_prospecting._utils` to ensure a single
hostile source never crashes the run.

Build order (Slice 1 vertical):
  phase_1_title         — MOD-IV title lookup, death-signal detection
  phase_2_genealogy     — heir discovery (obit, find-a-grave, legacy)
  phase_2_5_verification — heir living/deceased verification
  phase_3_target        — decision-maker selection
  phase_skiptrace       — phones + emails via TPS/FPS/CBC
"""
