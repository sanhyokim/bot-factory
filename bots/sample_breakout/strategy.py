"""直近20本の高値を、今の足の終値が上に抜けたら買う。出るのは利益確定(+1.0%)か損切り(−0.6%)だけ。"""

LOOKBACK = 20


def entry_signal(history):
    if len(history) <= LOOKBACK:
        return None
    prior_high = max(history.highs(LOOKBACK + 1)[:-1])
    if history[-1].c > prior_high:
        return "long"
    return None
