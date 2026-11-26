#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
    print "Set up Python using the macOS instructions in README.md, then open this test again."
    exit 1
fi
./.venv/bin/python capture_email.py --test-schedule
result=$?
read -r '?Press Return to close the test.'
exit "$result"
