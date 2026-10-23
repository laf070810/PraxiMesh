"""Small real end-to-end professional probes, evaluated only by trusted hosts."""

from judge import ml, scientific

ML_REFERENCE = """def train(features, targets):
    n = len(targets)
    mx = sum(row[0] for row in features) / n
    my = sum(targets) / n
    slope = sum((row[0]-mx)*(y-my) for row,y in zip(features,targets)) / sum((row[0]-mx)**2 for row in features)
    return {"weights": [slope], "bias": my-slope*mx}
"""
SCIENCE_REFERENCE = """def fit(x, y, sigma):
    import math
    w = [1/s**2 for s in sigma]
    sw = math.fsum(w)
    mx = math.fsum(a*b for a,b in zip(w,x))/sw
    my = math.fsum(a*b for a,b in zip(w,y))/sw
    centered = math.fsum(a*(b-mx)**2 for a,b in zip(w,x))
    b = math.fsum(a*(xx-mx)*(yy-my) for a,xx,yy in zip(w,x,y))/centered
    return {"parameters": [my-b*mx,b], "covariance": [[1/sw+mx*mx/centered,-mx/centered],[-mx/centered,1/centered]]}
"""


def ml_problem():
    cases = []
    for index, public in enumerate((True, False)):
        case = {
            "name": f"ml-probe-{index}",
            "public": public,
            "dataset": {"id": f"ml-probe-{index}", "revision": 1, "seed": index},
            "train": {
                "features": [[x] for x in range(4)],
                "targets": [2 + 3 * x for x in range(4)],
            },
            "holdout": {
                "features": [[x] for x in range(4, 8)],
                "targets": [2 + 3 * x for x in range(4, 8)],
            },
            "thresholds": {"mse": 1e-12, "mae": 1e-6},
        }
        case["dataset"]["digest"] = ml.dataset_digest(case)
        cases.append(case)
    problem = {
        "evaluator": ml.EVALUATOR,
        "language": "python",
        "function": "train",
        "model_kind": ml.MODEL_KIND,
        "tests": cases,
    }
    ml.validate_problem(problem)
    return problem


def science_problem():
    cases = []
    for index, public in enumerate((True, False)):
        case = {
            "name": f"science-probe-{index}",
            "public": public,
            "dataset": {"id": f"science-probe-{index}", "revision": 1, "seed": index},
            "observations": {"x": [0, 1, 2, 3], "y": [2.1, 4.8, 8.1, 11], "sigma": [1, 2, 1, 2]},
        }
        case["dataset"]["digest"] = scientific.dataset_digest(case)
        cases.append(case)
    problem = {
        "evaluator": scientific.EVALUATOR,
        "language": "python",
        "function": "fit",
        "uncertainty_model": scientific.UNCERTAINTY_MODEL,
        "tests": cases,
    }
    scientific.validate_problem(problem)
    return problem


def probes():
    return [
        (
            "ml/reference",
            {"problem": ml_problem(), "source": ML_REFERENCE},
            "accepted",
            None,
            "specialist",
        ),
        (
            "ml/forged-score",
            {
                "problem": ml_problem(),
                "source": 'def train(x,y): return {"weights":[0],"bias":0,"mse":0,"mae":0}',
            },
            "wrong_answer",
            None,
            "specialist",
        ),
        (
            "science/reference",
            {"problem": science_problem(), "source": SCIENCE_REFERENCE},
            "accepted",
            None,
            "specialist",
        ),
        (
            "science/invented-covariance",
            {
                "problem": science_problem(),
                "source": SCIENCE_REFERENCE.replace(
                    '"covariance": [[1/sw+mx*mx/centered,-mx/centered],[-mx/centered,1/centered]]',
                    '"covariance": [[0,0],[0,0]]',
                ),
            },
            "wrong_answer",
            None,
            "specialist",
        ),
    ]
