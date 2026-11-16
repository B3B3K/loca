PHASE 3 TRIAGE. Repeat: call next_candidates (n=5). For each candidate decide TRUE POSITIVE or FALSE POSITIVE:
read more context with read when the snippet is not enough, and follow the data from its source to the sink.
True positive: report_finding with candidate_id, exact evidence, impact and a concrete fix.
False positive (test code, constant input, already sanitised, dead code): dismiss with a short reason.
Handle several candidates per call to dismiss when they share a reason. Stop when nothing is pending or steps run
low, then finish with how many were reported and dismissed.
