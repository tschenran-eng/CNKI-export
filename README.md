# CNKI Metadata Exporter

Windows GUI tool for exporting CNKI metadata in batches with persistent institutional login, selectable export fields, and batch-level resume.

## Windows build

GitHub Actions builds a portable Windows package containing `CNKI_Metadata_Exporter.exe`.

The packaged application uses an installed **Microsoft Edge** first, then Google Chrome as fallback, so it does not need to download a separate Playwright Chromium browser.

Default export fields:

- 题名
- 发表时间 / 出版日期
- 关键词
- 摘要

Optional fields include authors, source, affiliation, fund, DOI and URL.

## Resume behavior

Completed XLS batches are skipped automatically. If a 500-record batch is interrupted part-way through, only that current batch is repeated on the next run; earlier completed batches are preserved.

Security verification / CAPTCHA is handled manually in the visible browser; the program waits for the user to complete it and then continues.
