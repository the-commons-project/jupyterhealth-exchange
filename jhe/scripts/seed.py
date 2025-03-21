# This script can be used to import the db/seed.sql data.

import os

import psycopg2

# Database connection parameters
db_params = {
    'dbname': os.environ.get('DB_NAME'),
    'user': os.environ.get('DB_USER'),
    'password': os.environ.get('DB_PASSWORD'),
    'host': os.environ.get('DB_HOST'),
    'port': os.environ.get('DB_PORT', 5432)
}

# Connect to the database
conn = psycopg2.connect(**db_params)
conn.autocommit = True
cursor = conn.cursor()

# Read and execute the SQL file
with open('/app/seed.sql', 'r') as sql_file:
    sql_commands = sql_file.read()
    
    # Execute the SQL commands
    cursor.execute(sql_commands)

# Close the connection
cursor.close()
conn.close()

print("Seed data imported successfully.")
