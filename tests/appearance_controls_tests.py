# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression for inherited dark button text on secondary backgrounds."""
import ast
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
import appearance as ap
import ui
import poster_filters
repo = Path(__file__).resolve().parents[1]
app_tree = ast.parse((repo/'preludarr/app.py').read_text(encoding='utf-8'))
app_css = next(ast.literal_eval(n.value) for n in app_tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CSS' for t in n.targets))
picker = (repo/'preludarr/poster_manual.py').read_text(encoding='utf-8')
filters = (repo/'preludarr/poster_filters.py').read_text(encoding='utf-8')
def rule(source, selector):
    return selector + '{' + re.search(re.escape(selector) + r'\{([^{}]*)\}', source).group(1) + '}'
modal_base = '#themearr-poster-modal button'
modal_secondary = '#themearr-poster-modal button.secondary'
primary = 'html body main .tm-btn.tm-btn.tm-btn.tm-primary'
secondary = 'html body main .tm-btn.tm-btn.tm-btn.tm-secondary'
sources = [(poster_filters.modal_script(), modal_base), (poster_filters.modal_script(), modal_secondary),
           (filters, 'button,.back'), (filters, '.back'), (picker, '.tp-secondary'),
           (app_css, 'button'), (app_css, '.view-toggle button'), (ui.UI_CSS, primary),
           (ui.UI_CSS, primary+':hover'), (ui.UI_CSS, secondary), (ui.UI_CSS, secondary+':hover')]
css = ''.join(rule(source, selector) for source, selector in sources)
pairs = [('Modal confirm', [modal_base]), ('Modal cancel', [modal_base, modal_secondary]),
         ('Fallback cancel', ['button,.back', '.back']), ('Picker cancel', ['button', '.tp-secondary']),
         ('Grid/List', ['button', '.view-toggle button']), ('Settings primary', [primary]),
         ('Settings primary hover', [primary, primary+':hover']), ('Settings secondary', [secondary]),
         ('Settings secondary hover', [secondary, secondary+':hover'])]
failures = []
count = 0
for theme in list(ap.STANDARD)+list(ap.IMMERSIVE):
    for scheme, oled, prefers in [('dark',False,''), ('dark',True,''), ('light',False,''), ('auto',False,'light'), ('auto',True,'dark')]:
        page = ap.apply('<html><head><style>'+css+'</style></head><body></body></html>',
                        {'scheme':scheme,'oled':oled,'theme':theme},prefers=prefers)
        for label, selectors in pairs:
            values = {}
            for selector in selectors:
                body = re.search(re.escape(selector)+r'\{([^{}]*)\}',page).group(1)
                for prop, val in re.findall(r'(?:^|;)\s*(background|color)\s*:\s*(#[0-9a-fA-F]{3,8}|white|black)',body):
                    values[prop] = ap._value_to_hex(val)[0]
            ratio = ap.contrast(values['color'],values['background'])
            count += 1
            if ratio < 4.5:
                failures.append('%s / %s / %s / OLED=%s: %.2f' % (label,theme,scheme,oled,ratio))
print(('PASS ' if not failures else 'FAIL ')+'Control foreground/background contrast across all appearance themes (%s combinations)' % count)
for failure in failures[:15]:print(failure)
print('%s/1 passed' % int(not failures))
sys.exit(bool(failures))
