"""Run the signal engine and produce a complete report from fictional CRM records."""
import json
from pathlib import Path

from signals import compute, classify, prepare_actions, to_dt
from preview_recap import render_recap, render_html, slack_messages


def main():
    root = Path(__file__).resolve().parent
    source = json.loads((root / 'examples/fictional-pipeline.json').read_text())
    rows = []
    for record in source['records']:
        deal = record['deal']
        signals = compute(deal, record['engagements'], now=to_dt(source['as_of']),
                          stage_labels={'evaluation': 'Évaluation technique'}, is_automated=lambda e: False)
        rows.append({'deal_id': deal['id'], 'dealname': deal['properties']['dealname'],
                     'stage': signals['stage_label'], 'bucket': classify(signals),
                     'analyzed_at': source['as_of'], 'signals': signals,
                     'coverage': 'direct_associations_only', 'fictional_example': True,
                     'prepared_actions': prepare_actions(signals)})
    output = root / 'output'
    output.mkdir(exist_ok=True)
    summary, blocks = render_recap(rows, timezone="Europe/Paris")
    (output / 'demo-analysis.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    (output / 'demo-recap.json').write_text(json.dumps({'preview_only': True, 'messages': slack_messages(summary, blocks)}, ensure_ascii=False, indent=2))
    path = output / 'demo-recap.html'
    path.write_text(render_html(rows, summary, timezone="Europe/Paris"))
    print(summary)
    print(path)


if __name__ == '__main__':
    main()
