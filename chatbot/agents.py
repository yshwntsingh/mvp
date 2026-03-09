
def planner_agent(query):
    if "why" in query.lower():
        return "explain"
    if "how" in query.lower():
        return "steps"
    return "direct"

def verifier_agent(answer):
    return "███" not in answer
