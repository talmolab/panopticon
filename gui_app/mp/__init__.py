"""Multi-process capture: the pieces that cross a process boundary.

`capture_processes: 0` (the default) keeps every camera in the GUI process
and does not import this package. A nonzero value splits the cameras across
worker processes; each worker grabs and encodes its own cameras and shares
only small records with the parent through named shared memory.

- `shm`: aligned numpy views, named segments, seqlock slots, and the status,
  preview and full-resolution segments.
- `ledger`: the kick-out ledger. The parent's `Coordinator` is the only
  writer of release decisions; each worker's `WorkerLedger` announces what
  its camera grabbed and reads back what to encode.

Importing `shm` (and so `ledger`) raises `shm.UnsupportedPlatform` on any CPU
other than x86-64: the protocol relies on aligned 8-byte loads and stores
being atomic and on stores becoming visible in program order.
"""
