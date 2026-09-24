"""Compatibility entrypoint for the PostgreSQL-backed index projection worker."""

from invoice_intelligence.workers.index_projection import main

if __name__ == "__main__":
    main()
