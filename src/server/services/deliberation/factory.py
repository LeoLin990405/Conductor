def get_store():
    from src.server.database.autonomy.deliberations import PostgresDeliberationStore
    return PostgresDeliberationStore()
