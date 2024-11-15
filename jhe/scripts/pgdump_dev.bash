#!/usr/bin/env bash
if [[ $(basename $(pwd)) == "scripts" ]]; then
  echo -e "\nRun this script from the parent directory\n"
  exit 1
fi
if [ ! -f ".env" ]; then
  echo -e "\nCan not find .env\n"
  exit 2
fi
export $(cat .env | sed 's/ /-/g' | sed 's/#.*//g' | xargs)
PGPASSWORD=$DB_PASSWORD pg_dump -U $DB_USER -h $DB_HOST -p $DB_PORT -d $DB_NAME -F plain --column-inserts --data-only > dump.txt
