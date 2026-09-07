"""Clarify legacy terminal errors without changing outcomes, quotas or receipts."""
import argparse
from cbrs.jobs import JobStore


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('database')
    args=parser.parse_args()
    store=JobStore(args.database)
    with store.connect() as db:
        count=db.execute("""UPDATE jobs SET error_message=?
            WHERE status='failed' AND error_code IN ('search_outcome_unknown','search_receipt_incomplete')""",
            ('Search outcome could not be confirmed. No PDF or empty result can be certified; search was not replayed.',)).rowcount
    print('Clarified terminal errors:',count)


if __name__=='__main__':
    main()
