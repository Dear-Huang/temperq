# -*- coding: utf-8 -*-
"""TemperQ 记录器：让回测顺便留下审计需要的每日打分。

把本文件全部内容粘贴到策略代码最前面，然后在策略给候选排序的地方加一行：

    temperq_record(context, scores, selected)

  scores   : {代码: 分数} —— 当天全部候选的分数，不只选中的
  selected : 当天实际选中的代码列表

回测结束后，聚宽/米筐会把 temperq_scores.csv 留在研究环境文件里（本地 RQAlpha 写在当前目录）。
再在研究环境运行 temperq_supplement.py，补上候选池收盘价和成交日分钟价。

只追加记录，不改变选股和下单。TemperQ 的 `qwb audit instrument-check` 可以核对这一点。
"""

TEMPERQ_FILE = 'temperq_scores.csv'
_temperq_rows = []


def _temperq_write(text, append):
    try:
        write_file(TEMPERQ_FILE, text, append=append)          # 聚宽、米筐云端
    except NameError:
        with open(TEMPERQ_FILE, 'a' if append else 'w', encoding='utf-8') as f:   # 本地 RQAlpha 等
            f.write(text)


def temperq_record(context, scores, selected=()):
    """记录一次排序。异常只记日志，绝不影响策略本身。"""
    try:
        now = context.current_dt if hasattr(context, 'current_dt') else context.now
        day, clock = now.strftime('%Y-%m-%d'), now.strftime('%H:%M:%S')
        chosen = set(selected or ())
        lines = []
        if not _temperq_rows:
            lines.append('date,code,score,selected,available_at\n')
        for code, score in dict(scores).items():
            try:
                value = float(score)
            except (TypeError, ValueError):
                continue
            if value != value:            # NaN
                continue
            lines.append(f'{day},{code},{value:.10g},{"true" if code in chosen else "false"},{day} {clock}\n')
        if lines:
            _temperq_rows.append(len(lines))
            _temperq_write(''.join(lines), append=len(_temperq_rows) > 1)
    except Exception as exc:              # the recorder must never change the strategy's behaviour
        try:
            log.warn('temperq_record 记录失败：%s' % exc)
        except Exception:
            pass
