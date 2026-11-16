- manifest {}                                   package, permissions, services/activities, BLE features.
- ble_scan {}                                   all 128-bit UUIDs (with files) + files ranked by BLE API usage.
- http_scan {}                                  URLs and Retrofit-style endpoints with files.
- find {pattern, [path], [glob], [ignore_case=true], [files_only=false], [limit=40], [all=false]}   regex search in src.
                                                Library code (androidx, kotlin, com/google, okhttp...) is hidden unless all=true.
                                                Prefer specific patterns: UUID\.fromString, "[0-9a-f]{8}-[0-9a-f]{4}-", writeCharacteristic.
- read {file, [start=1], [end], [where="src"|"out"]}   numbered lines (max 250 per call). Paths are relative to the root.
- ls {[path], [where]}                          directory listing.
- calc {op, ...}                                op="jbytes": {"values":"-86, 1, 0x10"} -> hex (Java bytes are SIGNED).
                                                op="hex2jbytes": {"hex":"aa0110"}.
                                                op="crc": {"hex":"<frame bytes>", "expect":"<checksum hex>"} brute-forces
                                                  algorithm x byte-range x endianness; without expect prints all algos.
- note {text}                                   durable finding WITH evidence (file:line). Notes are re-shown every turn
                                                and survive context pruning and restarts. Record everything important.
- write_file {path, content, [append=false]}    write into out. Keep each file under ~120 lines; use many small files.
                                                .py files are syntax-checked automatically.
- finish {report}                               final report (what was recovered, what is UNVERIFIED, next steps).
