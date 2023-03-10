from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.amazon.aws.sensors.s3 import S3KeySensor
import sqlite3
import pandas as pd
import os


def download_data_from_s3():
    """
    Downloads the data from Amazon S3 and stores it in the AutoSleep Data folder
    """
    autosleep_folder = 'AutoSleep Data/'

    source_s3 = S3Hook(s3_conn)
    source_s3.download_file(key=key, bucket_name=bucket_name, local_path=autosleep_folder,
                            preserve_file_name=True, use_autogenerated_subdir=False)

    print('Data has been downloaded to the AutoSleep Data folder')


def extract_data():
    """
    Extracts the data from the .csv files to include only the columns needed
    :return: dataframe with the columns including: date, wakeup time, time slept, efficiency, quality,
    deep sleep and SpO2 average.
    """

    df = pd.read_csv(f'AutoSleep Data/data.csv',
                     usecols=['toDate', 'waketime', 'asleep', 'efficiency', 'quality', 'deep', 'SpO2Avg'])

    return df


def remove_empty_spo2(data):
    """
    SpO2 can sometimes have no values (NaN) because the Sleep focus mode was not activated on the Apple Watch
    These entries will still be included in the data because they include data associated with sleep
    :param data: dataframe with NaN values
    :return: dataframe without NaN values
    """
    full_data = data[data['SpO2Avg'].notna()].copy()

    return full_data


def convert_date(data):
    """
    Converts dates to include the month with its numeric value before formatting the strings to datetime
    :param data: dataframe with month names
    :return: dataframe with month numbers
    """
    dates = {
        'Jan': '01',
        'Feb': '02',
        'Mar': '03',
        'Apr': '04',
        'May': '05',
        'Jun': '06',
        'Jul': '07',
        'Aug': '08',
        'Sep': '09',
        'Oct': '10',
        'Nov': '11',
        'Dec': '12',
    }

    # converting months to numeric values and slicing to only include the date
    data['toDate'] = data['toDate'].replace(dates, regex=True).str.slice(-11)

    # removing unnecessary characters
    data['toDate'] = data['toDate'].str.replace(',', '').str.replace(' ', '', 1)

    # converting the string to datetime format
    data['toDate'] = pd.to_datetime(data['toDate'], dayfirst=True).dt.strftime("%Y-%m-%d")

    return data


def finalise_data(data):
    """
    Transforming the data so that it is useful and all rows have data
    :param data: dataframe extracted with extract_data()
    :return: data that has been cleaned and formatted for SQL database
    """
    transformed = remove_empty_spo2(data)
    convert_date(transformed)

    # slicing the string because it includes the date as well
    # cannot use datetime (%H:%M:%S) format as it is stored as a string
    transformed['waketime'] = transformed['waketime'].str.slice(11, 19)

    # renaming columns to match database
    transformed.columns = ['date', 'wakeup_time', 'hours_slept', 'sleep_efficiency', 'quality_sleep_time',
                           'deep_sleep_time', 'oxygen_saturation_average']

    # rearranging column layout to include percentage values at the end
    transformed = transformed[['date', 'wakeup_time', 'hours_slept', 'quality_sleep_time',
                               'deep_sleep_time', 'sleep_efficiency', 'oxygen_saturation_average']]

    return transformed


def extract_and_transform():
    # extracting and transforming the data
    extracted_data = extract_data()
    transformed_data = finalise_data(extracted_data)

    # exporting to CSV in case xcom push and xcom pull do not like the size of data being passed around
    transformed_data.to_csv('AutoSleep Data/transformed_data.csv', index=False)
    print('Data has been exported as transformed_data.csv in the AutoSleep Data folder')


def load_to_sqlite():
    # datetime.now() used to get year and month for database and table names only
    now = datetime.now()
    month = now.month - 1   # -1 because @monthly schedule runs on first day of each month
    this_year = now.year

    # creating a connection to SQL database
    connection = sqlite3.connect(f'database/etl_autosleep_{this_year}')
    cursor = connection.cursor()

    print(f'Database: etl_autosleep_{this_year}')
    print(f'Month: {month}')

    # executing query to create a table in the database, and to append the data from dataframe onto it
    sql_query = f'CREATE TABLE IF NOT EXISTS autosleep_{month}' \
                f'(date DATE PRIMARY KEY, wakeup_time TIME,hours_slept TIME, quality_sleep_time TIME, ' \
                f'deep_sleep_time TIME, sleep_efficiency VARCHAR(5), oxygen_saturation_average VARCHAR(5))'

    cursor.execute(sql_query)

    transformed_data = pd.read_csv('AutoSleep Data/transformed_data.csv')
    transformed_data.to_sql(f'autosleep_{month}', connection, if_exists='append', index=False)

    connection.close()
    print(f'Database successfully updated with AutoSleep data from this month, stored as autosleep_{month}!')


def remove_temp_files():
    if os.path.exists('AutoSleep Data/data.csv'):
        os.remove('AutoSleep Data/data.csv')
        os.remove('AutoSleep Data/transformed_data.csv')

        print('Removed data.csv and transformed_data.csv from AutoSleep Data folder')


default_args = {
    'owner': 'airflow',
    'start_date': datetime(2023, 2, 1),
    'depends_on_past': False,
    'retries': 3,
    'retry_delay': timedelta(minutes=1),
}

with DAG(
    default_args=default_args,
    dag_id='autosleep_data_to_db',
    description='Extracts data from CSV and stores it on a database',
    schedule='@monthly',
    catchup=False,
) as dag:

    bucket_name = 'etl-airflow-autosleep'
    key = 'data.csv'
    s3_conn = 's3_conn'

    # Check if a file exists in S3
    s3_sensor = S3KeySensor(
        task_id="s3_sensor",
        poke_interval=60,
        timeout=60 * 5,
        soft_fail=False,
        bucket_name=bucket_name,
        bucket_key=key,
        aws_conn_id=s3_conn
    )

    download_data_from_s3 = PythonOperator(
        task_id='download_data_from_s3',
        python_callable=download_data_from_s3,
    )

    extract_and_transform = PythonOperator(
        task_id='extract_and_transform',
        python_callable=extract_and_transform,
    )

    load_to_sqlite = PythonOperator(
        task_id='load_to_sqlite',
        python_callable=load_to_sqlite,
    )

    remove_temp_files = PythonOperator(
        task_id='remove_temp_files',
        python_callable=remove_temp_files,
    )

s3_sensor >> download_data_from_s3 >> extract_and_transform >> load_to_sqlite >> remove_temp_files

