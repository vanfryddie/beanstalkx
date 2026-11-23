"""The chart macros: do they draw, and do they survive thin data.

Every chart on these pages is hand-built SVG, so there is no library to
trust. The cases worth testing are the ones a real site hands you in
week one: one data point, no data points, every value identical, and a
negative number where the macro expected a bar.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import application

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def render(body, **ctx):
    """One macro, rendered the way a page renders it."""
    tpl = "{% import '_charts.html' as viz %}" + body
    with application.application.test_request_context("/"):
        return application.application.jinja_env.from_string(tpl).render(**ctx)


def widths(svg):
    return [float(w) for w in re.findall(r"width:\s*(-?[\d.]+)%", svg)]


def points(label, value):
    return {"label": label, "value": value}


print("1. Sparkline")
out = render("{{ viz.sparkline(values) }}", values=[10, 14, 9, 22, 18])
check("it draws a polyline", "<polyline" in out)
check("with a point per value", len(re.findall(r"[\d.]+,[\d.]+", out)) >= 5)
check("the last point is marked", "<circle" in out)
check("it is labelled for a screen reader", 'aria-label="Trend across' in out)
check("nothing is drawn outside the box",
      all(0 <= float(y) <= 28 for y in re.findall(r"\d+\.\d+,(\d+\.\d+)", out)))
# One point is not a trend, so the macro deliberately draws nothing
# rather than a dot pretending to be one.
one = render("{{ viz.sparkline(values) }}", values=[7])
check("a single point draws nothing rather than a line to nowhere", "<svg" not in one, one[:80])
check("and no data draws nothing", "<svg" not in render("{{ viz.sparkline(values) }}", values=[]))
flat = render("{{ viz.sparkline(values) }}", values=[5, 5, 5, 5])
check("a flat series does not divide by zero", "<svg" in flat and "nan" not in flat.lower())
holes = render("{{ viz.sparkline(values) }}", values=[3, None, 8, None, 2])
check("gaps are dropped, not drawn as zero",
      "<svg" in holes and "None" not in holes and "nan" not in holes.lower())

print("\n2. Stat tile")
out = render("{{ viz.stat('Trained', '1,204', sub='vs last month', series=[1,4,2,9]) }}")
check("the label is there", "Trained" in out)
check("the value is there", "1,204" in out)
check("the sub-line is there", "vs last month" in out)
check("the sparkline rides along", "<svg" in out)
check("a tone becomes a class, not a colour",
      'is-warn' in render("{{ viz.stat('A', '1', tone='warn') }}"))
bare = render("{{ viz.stat('Trained', '0') }}")
check("a tile with nothing but a number still renders", "Trained" in bare and "<svg" not in bare)

print("\n3. Area chart")
series = [points("W40", 10), points("W41", 25), points("W42", 18)]
out = render("{{ viz.area_chart('Trained records', pts, unit=' records') }}", pts=series)
check("it has a title", "Trained records" in out)
check("it fills an area", "<polygon" in out)
check("and draws the line on top", "<polyline" in out)
check("the ends of the axis are labelled", ">W40<" in out and ">W42<" in out)
check("only the last point carries a number", out.count("viz-last") == 1)
check("that number is the last value", "18 records" in out)
check("every point has a hover target", out.count('class="viz-dot"') == 3)
check("the crosshair is wired up", "data-viz-crosshair" in out)
check("there is a tooltip to fill", "viz-tip" in out)
check("the grid is recessive, not a frame", out.count("viz-grid") == 3)
check("the scale starts at zero when the data is near it",
      "0,100" in out, out[out.find("<polygon"):out.find("<polygon") + 60])
single = render("{{ viz.area_chart('One', pts) }}", pts=[points("W1", 4)])
check("one point says so instead of drawing a trend",
      "Not enough periods" in single and "<polyline" not in single)
check("and so does none",
      "Not enough periods" in render("{{ viz.area_chart('None', pts) }}", pts=[]))
zeroes = render("{{ viz.area_chart('Zero', pts) }}",
                pts=[points("W1", 0), points("W2", 0)])
check("an all-zero series draws a flat floor rather than dividing by zero",
      "<polyline" in zeroes and "nan" not in zeroes.lower())
holey = render("{{ viz.area_chart('Holes', pts) }}",
               pts=[points("W1", 5), {"label": "W2", "value": None}, points("W3", 9)])
check("a missing period is skipped, not plotted as zero",
      "None" not in holey and holey.count('class="viz-dot"') == 2)

print("\n4. Diverging bars")
rows = [{"name": "Pick", "value": -42}, {"name": "Rebin", "value": 0},
        {"name": "Decant", "value": 17}]
out = render("{{ viz.diverging_bars('Gap to target', rows) }}", rows=rows)
check("every row is named", all(r["name"] in out for r in rows))
check("under and over get the two categorical hues, not red and green",
      "is-under" in out and "is-over" in out)
check("the sign is printed", "+17" in out and "-42" in out)
check("there is one baseline", out.count("viz-div-axis") == 3)
check("no bar is drawn with a negative width",
      all(w >= 0 for w in widths(out)), str(widths(out)))
check("the widest bar reaches the edge of its half",
      any(abs(w - 50) < 0.01 for w in widths(out)), str(widths(out)))
check("a long list is cut rather than scrolled forever",
      render("{{ viz.diverging_bars('T', rows, limit=2) }}", rows=rows).count("viz-div-row") == 2)
allzero = render("{{ viz.diverging_bars('Flat', rows) }}", rows=[{"name": "A", "value": 0}])
check("all-zero does not divide by zero", "viz-div-row" in allzero)
check("and an empty list says so", "Nothing to compare" in
      render("{{ viz.diverging_bars('T', rows) }}", rows=[]))

print("\n5. Meter")
out = render("{{ viz.meter(86.5, 100, label='To plan') }}")
check("the value is printed", "86.5%" in out)
check("the label rides in the tooltip", "To plan" in out)
check("short of the mark is a class", "is-short" in out)
check("and at the mark is another", "is-good" in render("{{ viz.meter(99, 100) }}"))
check("the fill is the share", any(abs(w - 86.5) < 0.1 for w in widths(out)), str(widths(out)))
over = render("{{ viz.meter(140, 100, label='Over') }}")
check("past the end clamps to the track", all(w <= 100.0 for w in widths(over)), str(widths(over)))
check("but the real number is still printed", "140%" in over)
under = render("{{ viz.meter(-5, 100) }}")
check("below zero does not render a negative width",
      all(w >= 0 for w in widths(under)), str(widths(under)))
nodata = render("{{ viz.meter(None, 100, label='No plan') }}")
check("no value shows a dash, not None", "—" in nodata and "None" not in nodata)
check("and an empty track", all(w == 0 for w in widths(nodata)), str(widths(nodata)))

print("\n6. Split bar and legend")
parts = [{"label": "Own department", "share": 0.0833, "text": "300h"},
         {"label": "Cross-trained in", "share": 0.9167, "text": "3,300h"}]
out = render("{{ viz.split_bar(parts) }}", parts=parts)
check("both segments are drawn", out.count("viz-split-part") == 2, out[:200])
check("the widths are the shares", abs(sum(widths(out)) - 100) < 0.5, str(widths(out)))
check("the smaller share is actually smaller", widths(out)[0] < widths(out)[1], str(widths(out)))
check("each segment carries its own figure", "300h" in out and "3,300h" in out)
check("a zero segment is left out rather than drawn at nothing",
      render("{{ viz.split_bar(parts) }}",
             parts=[{"label": "A", "share": 0, "text": "0h"}]).count("viz-split-part") == 0)
leg = render("{{ viz.legend(['Own department', 'Cross-trained in']) }}")
check("the legend names both", "Own department" in leg and "Cross-trained in" in leg)
check("so identity is never colour alone", leg.count("viz-swatch") == 2)
check("and the swatches match the segments in order",
      "viz-split-1" in leg and "viz-split-2" in leg)

print("\n7. The real pages")
application.application.config["TESTING"] = True
c = application.application.test_client()
with c.session_transaction() as s:
    s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", "SCN2"
for tab in ("insights", "model"):
    page = c.get(f"/ld-management/cross-training?tab={tab}", follow_redirects=True)
    body = page.get_data(as_text=True)
    check(f"{tab} renders", page.status_code == 200, str(page.status_code))
    check(f"{tab} draws charts", body.count("<svg") >= 3, str(body.count("<svg")))
    check(f"{tab} leaks no NaN", "nan" not in body.lower().replace("financial", ""))
    check(f"{tab} leaks no None", ">None<" not in body and "None%" not in body)
    check(f"{tab} has no negative widths",
          not re.search(r"width:\s*-", body), str(re.findall(r"width:\s*-[\d.]+%", body))[:80])
    # The crosshair binds itself once per plot and guards against a
    # second binding; a tab with no time series needs no script at all.
    plots = body.count("data-viz-crosshair")
    check(f"{tab} ships the crosshair exactly when it has plots",
          (plots > 0) == ("vizBound" in body), f"{plots} plots")
    check(f"{tab} includes the crosshair script only once",
          body.count("plot.dataset.vizBound = '1'") <= 1,
          str(body.count("plot.dataset.vizBound = '1'")))

print("\n" + "=" * 62)
print("ALL CHART CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
