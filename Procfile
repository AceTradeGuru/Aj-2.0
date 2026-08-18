# Two workers, not four: this is one person's app, and the SQLite database is a
# single file that does not enjoy being written from many processes at once.
web: gunicorn --workers 2 --timeout 120 --bind 0.0.0.0:$PORT --access-logfile - --error-logfile - app:app
