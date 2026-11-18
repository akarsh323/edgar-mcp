| Model | Tool surface | Output budget | Questions | Accuracy | Tool calls / question | Tokens / question | Tool output / question | Calls over budget | Excluded |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| edgar_mcp/scripted (scripted, not an LLM) | `designed` | 100,000 B | 24 | 100.0% [100.0, 100.0] | 1.00 | 430 | 0.5 KiB | 0.0% | 0 |
| edgar_mcp/scripted (scripted, not an LLM) | `designed` | 16,384 B | 24 | 100.0% [100.0, 100.0] | 1.00 | 430 | 0.5 KiB | 0.0% | 0 |
| edgar_mcp/scripted (scripted, not an LLM) | `baseline` | 100,000 B | 24 | 100.0% [100.0, 100.0] | 2.00 | 70,927 | 903.9 KiB | 87.5% | 0 |
| edgar_mcp/scripted (scripted, not an LLM) | `baseline` | 16,384 B | 24 | 0.0% [0.0, 0.0] | 1.50 | 8,614 | 843.1 KiB | 100.0% | 0 |

Accuracy by question type:

| Model | Tool surface | Output budget | `annual_figure` | `current_report_count` | `latest_annual_report` | `quarterly_filing_date` |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| edgar_mcp/scripted | `designed` | 100,000 B | 100.0% | 100.0% | 100.0% | 100.0% |
| edgar_mcp/scripted | `designed` | 16,384 B | 100.0% | 100.0% | 100.0% | 100.0% |
| edgar_mcp/scripted | `baseline` | 100,000 B | 100.0% | 100.0% | 100.0% | 100.0% |
| edgar_mcp/scripted | `baseline` | 16,384 B | 0.0% | 0.0% | 0.0% | 0.0% |
