#!/usr/bin/env python

import sys
from pathlib import Path

file = Path(__file__).resolve()
parent = file.parent
root = None
for parent in file.parents:
    if parent.name == "ampscz-formsqc":
        root = parent
sys.path.append(str(root))

# remove current directory from path
try:
    sys.path.remove(str(parent))
except ValueError:
    pass

import logging
from typing import List, Dict, Optional, Set
from datetime import datetime

import pandas as pd
from rich.logging import RichHandler

from formsqc.helpers import cli, db, utils, dpdash
from formsqc import constants

MODULE_NAME = "formsqc_cognitive_combined_exporter"

console = utils.get_console()

logger = logging.getLogger(MODULE_NAME)
logargs = {
    "level": logging.DEBUG,
    # "format": "%(asctime)s - %(process)d - %(name)s - %(levelname)s - %(message)s",
    "format": "%(message)s",
    "handlers": [RichHandler(rich_tracebacks=True)],
}
logging.basicConfig(**logargs)

no_upenn_data: Set[str] = set()
no_penncnb_data: Set[str] = set()
no_data: Set[str] = set()

warnings_cache: Set[str] = set()


def warn(msg: str) -> None:
    global warnings_cache

    if msg not in warnings_cache:
        logger.warning(msg)
        warnings_cache.add(msg)


def construct_output_filename(
    subject_id: str,
    df: pd.DataFrame,
) -> str:
    site_id = subject_id[:2]

    # start_day = int(df["day"].min()) if not df.empty else 1
    end_day = int(df["day"].max()) if not df.empty else 1

    optional_tags: List[str] = ["combined"]

    try:
        event_type = df["event_type"].unique()
    except KeyError:
        event_type = []

    if len(event_type) == 1:
        optional_tags.append(event_type[0])

    dpdash_name = dpdash.get_dpdash_name(
        study=site_id,
        subject=subject_id,
        data_type="forms",
        category="cognition",
        optional_tag=optional_tags,
        time_range=f"day1to{end_day}",
    )

    filename = f"{dpdash_name}.csv"

    return filename


def get_output_dir(config_file: Path) -> Path:
    output_params = utils.config(config_file, "outputs")

    output_dir = Path(output_params["cognitive_combined_outputs_root"])
    return output_dir


def get_penncnb_redcap_form(config_file: Path, subject_id: str) -> pd.DataFrame:
    query = f"""
    SELECT subject_id, event_name, form_data FROM forms
        WHERE subject_id = '{subject_id}' AND
            form_name = 'penncnb';
    """

    penncnb_form = db.execute_sql(config_file=config_file, query=query)

    return penncnb_form


def get_upenn_redcap_data(config_file: Path, subject_id: str) -> pd.DataFrame:
    query = f"""
    SELECT subject_id, event_name, event_type, form_data FROM upenn_forms
        WHERE subject_id = '{subject_id}';
    """

    upenn_form = db.execute_sql(config_file=config_file, query=query)

    return upenn_form


def explode_formdata(df: pd.DataFrame) -> pd.DataFrame:
    df = pd.concat([df.drop("form_data", axis=1), pd.json_normalize(df["form_data"])], axis=1)  # type: ignore

    return df


def map_event_names(verbose: List[str]) -> Dict[str, str]:  # type: ignore
    visit_orders = constants.upenn_visit_order

    def _function(verbose: List[str], event_names: List[str]):
        for verbose_name in verbose:
            for event_name in event_names:
                if event_name in verbose_name:
                    yield event_name, verbose_name  # type: ignore

    event_name_map = dict(_function(verbose, visit_orders))

    return event_name_map


def get_week_day(datetime: datetime) -> int:
    # Return the day of the week as an integer, where Saturday is 1 and Friday is 7.
    iso_weekday = datetime.isoweekday()

    if iso_weekday > 5:
        weekday = iso_weekday - 5
    else:
        weekday = iso_weekday + 2

    return weekday


def get_site_id(subject_id: str) -> Optional[str]:
    query = f"""
    SELECT site_id FROM subjects
        WHERE id = '{subject_id}';
    """

    site_id = db.fetch_record(config_file=config_file, query=query)

    return site_id


def generate_csv(
    config_file: Path,
    subject_id: str,
    output_dir: Path,
) -> None:
    global no_upenn_data
    global no_penncnb_data
    global no_data

    penncnb_form = get_penncnb_redcap_form(
        config_file=config_file, subject_id=subject_id
    )
    penncnb_form = explode_formdata(penncnb_form)

    upenn_form = get_upenn_redcap_data(config_file=config_file, subject_id=subject_id)
    upenn_form = explode_formdata(upenn_form)

    upenn_event_names = upenn_form["event_name"].unique().tolist()
    penncnb_event_names = penncnb_form["event_name"].unique().tolist()

    upenn_event_names_map = map_event_names(upenn_event_names)
    penncnb_event_names_map = map_event_names(penncnb_event_names)

    # Reverse dict
    upenn_event_names_map = {v: k for k, v in upenn_event_names_map.items()}

    if len(upenn_event_names_map) > 0:
        upenn_form["event_name"] = upenn_form["event_name"].map(upenn_event_names_map)
    if len(penncnb_event_names_map) > 0:
        upenn_form["event_name"] = upenn_form["event_name"].map(penncnb_event_names_map)

    # event_name_map = dict(map_verbose_event_names(event_names_verbose, event_names))

    # Replace upenn event names with penncnb verbose event names
    # upenn_form["event_name"] = upenn_form["event_name"].map(event_name_map)

    event_types = upenn_form["event_type"].unique().tolist()

    if len(event_types) == 0:
        # logger.warning(f"No upenn_form data found for {subject_id}.")
        event_types = [None]

    for event_type in event_types:
        event_type_df = upenn_form[upenn_form["event_type"] == event_type]

        if penncnb_form.empty and not event_type_df.empty:
            combined_df = event_type_df.copy()
            no_penncnb_data.add(subject_id)
        elif event_type_df.empty and not penncnb_form.empty:
            combined_df = penncnb_form.copy()
            no_upenn_data.add(subject_id)
        elif event_type_df.empty and penncnb_form.empty:
            no_data.add(subject_id)
            return
        else:
            combined_df = pd.merge(
                penncnb_form,
                event_type_df,
                on=["subject_id", "event_name"],
                how="inner",
            )

        # try:
        #     combined_df = pd.merge(
        #         penncnb_form,
        #         event_type_df,
        #         on=["subject_id", "event_name"],
        #         how="inner",
        #     )
        # except (ValueError, KeyError):
        #     # either penncnb_form or event_type_df is empty
        #     # Use available df
        #     if penncnb_form.empty and not event_type_df.empty:
        #         combined_df = event_type_df
        #         no_penncnb_data.add(subject_id)
        #     elif event_type_df.empty and not penncnb_form.empty:
        #         combined_df = penncnb_form
        #         no_upenn_data.add(subject_id)
        #     else:
        #         no_data.add(subject_id)
        #         return

        # Add col 'weekday'
        try:
            interview_date = pd.to_datetime(combined_df["chrpenn_interview_date"])
            combined_df["weekday"] = interview_date.apply(get_week_day)
        except KeyError:
            try:
                interview_date = pd.to_datetime(combined_df["chrpenn_entry_date"])
                combined_df["weekday"] = interview_date.apply(get_week_day)
            except KeyError:
                warn(f"No interview date found for {subject_id}.")
                combined_df["weekday"] = pd.NA

        # Add col 'site_id'
        subject_id_l = combined_df["subject_id"]
        combined_df["site_id"] = subject_id_l.apply(get_site_id)

        # Add col 'day'
        try:
            interview_dates = combined_df["chrpenn_interview_date"].tolist()
            for idx, interview_date in enumerate(interview_dates):
                event_date = datetime.strptime(interview_date, "%Y-%m-%dT%H:%M:%S")
                days_from_consent = utils.get_days_since_consent(
                    config_file=config_file,
                    subject_id=subject_id,
                    event_date=event_date,
                )
                combined_df.loc[idx, "day"] = days_from_consent
        except (KeyError, TypeError):
            # If no interview date found, use entry date instead
            try:
                entry_dates = combined_df["chrpenn_entry_date"].tolist()
                for idx, entry_date in enumerate(entry_dates):
                    event_date = datetime.strptime(entry_date, "%Y-%m-%dT%H:%M:%S")
                    days_from_consent = utils.get_days_since_consent(
                        config_file=config_file,
                        subject_id=subject_id,
                        event_date=event_date,
                    )
                    combined_df.loc[idx, "day"] = days_from_consent
            except (KeyError, TypeError):
                warn(f"No interview date found for {subject_id}.")
                # Use an ascending day number
                combined_df["day"] = range(1, combined_df.shape[0] + 1)
        except utils.NoSubjectConsentDateException:
            logger.warning(f"No consent date found for {subject_id}. Skipping...")
            return

        filename = construct_output_filename(subject_id=subject_id, df=combined_df)
        filepath = output_dir / filename

        combined_df.to_csv(filepath, index=False)


def export_data(config_file: Path, output_dir: Path) -> None:
    subject_query = """
        SELECT id FROM subjects ORDER BY id ASC;
    """

    subject_id_df = db.execute_sql(config_file, subject_query)
    subject_ids = subject_id_df["id"].tolist()

    subjects_count = len(subject_ids)
    logger.info(f"Exporting data for {subjects_count} subjects...")

    with utils.get_progress_bar() as progress:
        task = progress.add_task("Processing...", total=subjects_count)

        for subject_id in subject_ids:
            progress.update(task, advance=1, description=f"Processing {subject_id}...")

            generate_csv(
                config_file=config_file, subject_id=subject_id, output_dir=output_dir
            )


if __name__ == "__main__":
    console.rule(f"[bold red]{MODULE_NAME}")

    config_file = utils.get_config_file_path()
    config_params = utils.config(config_file, "general")
    console.print(f"Using config file: {config_file}")

    utils.configure_logging(
        config_file=config_file, module_name=MODULE_NAME, logger=logger
    )

    output_dir = get_output_dir(config_file=config_file)
    logger.info(f"Writing output to {output_dir}...")

    logger.warning("Clearing existing data...")
    cli.clear_directory(output_dir)

    logger.info("Exporting data...")
    export_data(config_file=config_file, output_dir=output_dir)

    if len(no_upenn_data) > 0:
        logger.warning(
            f"No upenn_form data found for {len(no_upenn_data)} subjects: {no_upenn_data}"
        )
    if len(no_penncnb_data) > 0:
        logger.warning(
            f"No penncnb_form data found for {len(no_penncnb_data)} subjects: {no_penncnb_data}"
        )
    if len(no_data) > 0:
        logger.warning(f"No data found for {len(no_data)} subjects: {no_data}")

    logger.info("Done.")
