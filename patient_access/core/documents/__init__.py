"""Phase 4, second pass: the document content the fetched resources point at.

    candidates  where documents are and which one of several formats to take (pure)
    budget      the caps, and the only place they are checked (pure)
    downloader  one document streamed into the spool, or a reason it was skipped (I/O)
    run         the pass: candidates through the budget and the downloader, one record each

It runs after every resource type, under the same token, so a timeout here never costs the
resources; and it is resumable: a candidate whose blob the spool already holds is not refetched.
"""
