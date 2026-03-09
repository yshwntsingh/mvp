
import datetime

def log_event(user, role, query, status):
    with open("audit.log", "a") as f:
        f.write(
            f"{datetime.datetime.utcnow()} | "
            f"{user} | {role} | {status} | {query}\n"
        )
