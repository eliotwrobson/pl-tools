# An earlier version of this script was originally published at
# https://github.com/ubc-cpsc/canvasgrading and has been migrated to this repository.

import json
import os
import re
import uuid
from collections import OrderedDict
from itertools import count
from typing import Any

import click
import requests

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"], show_default=True, max_content_width=120)


class Canvas:
    def __init__(self, config_file_name: str) -> None:
        self.config_file_name = config_file_name
        with open(os.path.join(os.path.dirname(__file__), config_file_name)) as config:
            self.config = json.load(config)
        self.token = self.config["access_token"]
        self.api_url = self.config["api_url"]
        self.token_header = {"Authorization": f"Bearer {self.token}"}

    def request(self, request: str, *, stop_at_first: bool = False) -> list[Any]:
        retval: list[Any] = []
        response = requests.get(self.api_url + request, headers=self.token_header)
        while True:
            response.raise_for_status()
            retval.append(response.json())
            if (
                stop_at_first
                or "current" not in response.links
                or "last" not in response.links
                or response.links["current"]["url"] == response.links["last"]["url"]
            ):
                break
            response = requests.get(response.links["next"]["url"], headers=self.token_header)
        return retval

    def courses(self) -> list[dict[str, Any]]:
        courses: list[dict[str, Any]] = []
        for result in self.request("/courses?include[]=term&state[]=available"):
            courses.extend(result)
        return courses

    def course(self, course_id: int | None) -> "Course":
        if course_id is not None:
            for course in self.request(f"/courses/{course_id}?include[]=term"):
                return Course(self.config_file_name, course)

        courses = self.courses()
        for index, course in enumerate(courses):
            term = course.get("term", {}).get("name", "NO TERM")
            course_code = course.get("course_code", "UNKNOWN COURSE")
            print(f"{index:2}: {course['id']:7} - {term:10} / {course_code}")
        course_index = int(input("Which course? "))
        return Course(self.config_file_name, courses[course_index])


class Course(Canvas):
    def __init__(self, config_file_name: str, course_data: dict[str, Any]) -> None:
        super().__init__(config_file_name)
        self.data = course_data
        self.id = course_data["id"]
        self.url_prefix = f"/courses/{self.id}"

    def __getitem__(self, key: str) -> Any:
        """Returns the specified key from the course data."""
        return self.data[key]

    def quizzes(self) -> list["Quiz"]:
        quizzes: list[Quiz] = []
        for result in self.request(f"{self.url_prefix}/quizzes"):
            quizzes += [Quiz(self.config_file_name, self, quiz) for quiz in result if quiz["quiz_type"] == "assignment"]
        return quizzes

    def quiz(self, quiz_id: int | None) -> "Quiz":
        if quiz_id is not None:
            for quiz in self.request(f"{self.url_prefix}/quizzes/{quiz_id}"):
                return Quiz(self.config_file_name, self, quiz)

        quizzes = self.quizzes()
        for index, quiz in enumerate(quizzes):
            print(f"{index:2}: {quiz['id']:7} - {quiz['title']}")
        quiz_index = int(input("Which quiz? "))
        return quizzes[quiz_index]


class CourseSubObject(Canvas):
    # If not provided, the request_param_name defaults to the lower-cased class name.
    def __init__(
        self,
        config_file_name: str,
        parent: "Course | CourseSubObject",
        route_name: str,
        data: dict[str, Any],
        id_field: str = "id",
        request_param_name: str | None = None,
    ) -> None:
        super().__init__(config_file_name)

        self.parent = parent
        self.data = data
        self.id_field = id_field
        self.id = self.compute_id()
        self.route_name = route_name
        self.url_prefix = self.compute_url_prefix()
        if not request_param_name:
            request_param_name = type(self).__name__.lower()
        self.request_param_name = request_param_name

    def get_course(self) -> Course:
        if isinstance(self.parent, Course):
            return self.parent
        else:
            return self.parent.get_course()

    def compute_id(self) -> str:
        return self.data[self.id_field]

    def compute_base_url(self) -> str:
        return f"{self.parent.url_prefix}/{self.route_name}"

    def compute_url_prefix(self) -> str:
        return f"{self.compute_base_url()}/{self.id}"

    def __getitem__(self, index: str) -> Any:
        """Returns the specified key from the object data."""
        return self.data[index]


class Quiz(CourseSubObject):
    def __init__(self, config_file_name: str, course: Course, quiz_data: dict[str, Any]) -> None:
        super().__init__(config_file_name, course, "quizzes", quiz_data)

    def question_group(self, group_id: str | None) -> dict[str, Any] | None:
        if not group_id:
            return None
        for group in self.request(f"{self.url_prefix}/groups/{group_id}"):
            return group
        return None

    def questions(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        # TODO Narrow these types
        questions: dict[str, dict[str, Any]] = {}
        groups: dict[str, dict[str, Any]] = {}
        i = 1
        for result in self.request(f"{self.url_prefix}/questions?per_page=100"):
            for question in result:
                if question["quiz_group_id"] in groups:
                    group = groups[question["quiz_group_id"]]
                else:
                    group = self.question_group(question["quiz_group_id"])
                    if group:
                        groups[question["quiz_group_id"]] = group

                if group:
                    question["points_possible"] = group["question_points"]
                    question["position"] = group["position"]
                else:
                    question["position"] = i
                    i += 1
                questions[question["id"]] = question

        for grp in groups.values():
            if not grp:
                continue

            for question in [
                q for q in questions.values() if q["position"] >= grp["position"] and q["quiz_group_id"] is None
            ]:
                question["position"] += 1

        return (
            OrderedDict(sorted(questions.items(), key=lambda t: t[1]["position"])),
            OrderedDict(sorted(groups.items(), key=lambda t: t[1]["position"])),
        )


def file_name_only(name: str) -> str:
    return re.sub(r"[\W_]+", "", name)


def clean_question_text(text: str) -> str:
    # Some Canvas plugins like DesignPlus inject custom CSS and JavaScript into
    # all questions. This code is not needed in PrairieLearn, and in fact can
    # cause problems in CBTF environments since they'll be forbidden from loading
    # by most firewalls/proxies. We just remove them.
    #
    # We use regex instead of a proper HTML parser because we want to limit this
    # script to only using the Python standard library.
    text = re.sub(r"<link[^>]*>", "", text)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text)

    return text


def image_file_extension(content_type: str) -> str:
    match content_type:
        case "image/x-icon":
            return "ico"
        case "image/svg+xml":
            return "svg"
        case _:
            return content_type.split("/")[1]


def handle_images(question_dir: str, text: str) -> str:
    # Links to images will still point to Canvas. We need to download them and
    # replace them with `pl-figure` elements.
    #
    # We use regex instead of a proper HTML parser because we want to limit this
    # script to only using the Python standard library.
    image_count = count(1)
    for match in re.finditer(r'(<p>)?(<img[^>]*src="([^"]+)"[^>]*>)(</p>)?', text):
        url = match.group(3)
        if not url.startswith("http"):
            continue

        # Set up the `clientFilesQuestion` directory for this question.
        client_files_question_dir = os.path.join(question_dir, "clientFilesQuestion")
        os.makedirs(client_files_question_dir, exist_ok=True)

        # Canvas image URLs don't include the file extension, so we need to
        # extract it from the `Content-Type` header.
        res = requests.get(url)
        res.raise_for_status()
        extension = image_file_extension(res.headers["Content-Type"])

        file_name = f"image_{next(image_count)}.{extension}"
        file_path = os.path.join(client_files_question_dir, file_name)
        with open(file_path, "wb") as f:
            f.write(res.content)

        # Extract the alt text, if any.
        alt_match = re.search(r'alt="([^"]*)"', match.group(2))
        alt_text = alt_match.group(1) if alt_match else ""
        alt_attribute = f' alt="{alt_text}"' if alt_text else ""

        # Canvas will sometimes wrap images in `<p>` tags, which we don't want.
        # We'll handle those by checking if an image is preceded by an opening
        # `<p>` tag and followed by a closing `</p>` tag. If so, we'll remove
        # those tags.
        replace_str = match.group(2)
        if match.group(1) == "<p>" and match.group(4) == "</p>":
            replace_str = match.group(0)

        # Replace the image with a `pl-figure` element.
        text = text.replace(
            replace_str,
            f'<pl-figure file-name="{file_name}"{alt_attribute}></pl-figure>',
            1,
        )

    return text


@click.command()
@click.option("--pl-repo", help="Directory where PrairieLearn repo is stored", required=True)
@click.option("--pl-course-instance", help="Course instance where assessment will be created", required=True)
@click.option(
    "-cfn",
    "--config-file-name",
    default="config.json",
    help="Name of config file to use.",
)
@click.option("-q", "--quiz-id", type=int, help="Quiz ID")
@click.option("-c", "--course-id", type=int, help="Course ID")
@click.option(
    "-t", "--assessment-type", default=None, help="Assessment type to assign this assessment to"
)  # Make this type either exam or hw
@click.option("-s", "--assessment-set", default="Quiz", help="Assessment set to assign this assessment to")
@click.option(
    "-n", "--assessment-number", default="", help="Assessment set to assign this assessment to"
)  # TODO some of these should definitely be restricted to ints
@click.option("--topic", default="None", help="Assessment set to assign this assessment to")
def main(
    pl_repo: str,
    pl_course_instance: str,
    config_file_name: str,
    quiz_id: int,
    course_id: int,
    assessment_type: str | None,
    assessment_set: str,
    assessment_number: str,
    topic: str,
) -> None:
    canvas_client = Canvas(config_file_name)

    if not os.path.exists(os.path.join(pl_repo, "infoCourse.json")):
        raise ValueError("Provided directory is not a PrairieLearn repository")

    print("Reading data from Canvas...")
    course = canvas_client.course(course_id)
    print("Using course: {} / {}".format(course["term"]["name"], course["course_code"]))

    quiz = course.quiz(quiz_id)
    print("Using quiz: {}".format(quiz["title"]))

    # Reading questions
    print("Retrieving quiz questions from Canvas...")
    (questions, groups) = quiz.questions()

    questions_dir = os.path.join(pl_repo, "questions", file_name_only(quiz["title"]))
    if not os.path.isdir(questions_dir):
        os.makedirs(questions_dir)
    assessments_dir = os.path.join(pl_repo, "courseInstances", pl_course_instance, "assessments")
    if not os.path.isdir(assessments_dir):
        os.makedirs(assessments_dir)

    quiz_name = os.path.join(assessments_dir, file_name_only(quiz["title"]))
    if os.path.exists(quiz_name):
        suffix = 1
        while os.path.exists(f"{quiz_name}_{suffix}"):
            suffix += 1
        quiz_name = f"{quiz_name}_{suffix}"
    os.makedirs(quiz_name)

    pl_quiz_allow_access_rule = {"credit": 100}

    if quiz["access_code"]:
        pl_quiz_allow_access_rule["password"] = quiz["access_code"]
    if quiz["unlock_at"]:
        pl_quiz_allow_access_rule["startDate"] = quiz["unlock_at"]
    if quiz["lock_at"]:
        pl_quiz_allow_access_rule["endDate"] = quiz["lock_at"]
    if quiz["time_limit"]:
        pl_quiz_allow_access_rule["timeLimitMin"] = quiz["time_limit"]

    pl_quiz_questions = []

    for question in questions.values():
        # Clear the screen
        if os.name == "nt":
            os.system("cls")
        else:
            print("\033c", end="")

        question_text = clean_question_text(question["question_text"])

        print(f"Handling question {question['id']}...")
        print(question_text)
        print()
        for answer in question.get("answers", []):
            print(f" - {answer['text']}")
        question_title = input("\nQuestion title (or blank to skip): ")
        if not question_title:
            continue
        question_name = file_name_only(question_title)
        suffix = 0
        while os.path.exists(os.path.join(questions_dir, question_name)):
            suffix += 1
            question_name = f"{file_name_only(question_title)}_{suffix}"
        question_dir = os.path.join(questions_dir, question_name)
        os.makedirs(question_dir)

        question_alt = {
            "id": file_name_only(quiz["title"]) + "/" + question_name,
            "points": question["points_possible"],
        }

        if question["quiz_group_id"]:
            group = groups[question["quiz_group_id"]]
            if "_pl_alt" not in group:
                group["_pl_alt"] = {
                    "numberChoose": group["pick_count"],
                    "points": group["question_points"],
                    "alternatives": [],
                }
                pl_quiz_questions.append(group["_pl_alt"])
            group["_pl_alt"]["alternatives"].append(question_alt)
        else:
            pl_quiz_questions.append(question_alt)

        with open(os.path.join(question_dir, "info.json"), "w") as info:
            obj = {
                "uuid": str(uuid.uuid4()),
                "type": "v3",
                "title": question_title,
                "topic": topic,
                "tags": ["fromcanvas"],
            }
            if question["question_type"] == "text_only_question" or question["question_type"] == "essay_question":
                obj["gradingMethod"] = "Manual"
            json.dump(obj, info, indent=2)

        # Handle images.
        question_text = handle_images(question_dir, question_text)

        with open(os.path.join(question_dir, "question.html"), "w") as template:
            if question["question_type"] == "calculated_question":
                for variable in question["variables"]:
                    question_text = question_text.replace(
                        f"[{variable['name']}]", "{{params." + variable["name"] + "}}"
                    )

            if (
                question["question_type"] != "fill_in_multiple_blanks_question"
                and question["question_type"] != "multiple_dropdowns_question"
            ):
                include_paragraph = not question_text.strip().startswith("<p>")
                template.write("<pl-question-panel>\n")
                if include_paragraph:
                    template.write("<p>\n")
                template.write(question_text + "\n")
                if include_paragraph:
                    template.write("</p>\n")
                template.write("</pl-question-panel>\n")

            if question["question_type"] == "text_only_question":
                pass

            elif question["question_type"] == "essay_question":
                template.write('<pl-rich-text-editor file-name="answer.html"></pl-rich-text-editor>\n')

            elif question["question_type"] == "multiple_answers_question":
                template.write('<pl-checkbox answers-name="checkbox">\n')
                for answer in question["answers"]:
                    if answer["weight"]:
                        template.write('  <pl-answer correct="true">')
                    else:
                        template.write("  <pl-answer>")
                    template.write(answer["text"] + "</pl-answer>\n")
                template.write("</pl-checkbox>\n")

            elif (
                question["question_type"] == "true_false_question"
                or question["question_type"] == "multiple_choice_question"
            ):
                template.write('<pl-multiple-choice answers-name="mc">\n')
                for answer in question["answers"]:
                    if answer["weight"]:
                        template.write('  <pl-answer correct="true">')
                    else:
                        template.write("  <pl-answer>")
                    template.write(answer["text"] + "</pl-answer>\n")
                template.write("</pl-multiple-choice>\n")

            elif question["question_type"] == "numerical_question":
                answer = question["answers"][0]
                if (
                    answer["numerical_answer_type"] == "exact_answer"
                    and abs(answer["exact"] - int(answer["exact"])) < 0.001
                    and answer["margin"] == 0
                ):
                    template.write(
                        f'<pl-integer-input answers-name="value" correct-answer="{int(answer["exact"])}"></pl-integer-input>\n'
                    )
                elif answer["numerical_answer_type"] == "exact_answer":
                    template.write(
                        f'<pl-number-input answers-name="value" correct-answer="{answer["exact"]}" atol="{answer["margin"]}"></pl-number-input>\n'
                    )
                elif answer["numerical_answer_type"] == "range_answer":
                    average = (answer["end"] + answer["start"]) / 2
                    margin = abs(answer["end"] - average)
                    template.write(
                        f'<pl-number-input answers-name="value" correct-answer="{average}" atol="{margin}"></pl-number-input>\n'
                    )
                elif answer["numerical_answer_type"] == "precision_answer":
                    template.write(
                        f'<pl-number-input answers-name="value" correct-answer="{answer["approximate"]}" comparison="sigfig" digits="{answer["precision"]}"></pl-number-input>\n'
                    )
                else:
                    input(f"Invalid numerical answer type: {answer['numerical_answer_type']}")
                    template.write('<pl-number-input answers-name="value"></pl-number-input>\n')

            elif question["question_type"] == "calculated_question":
                answers_name = question["formulas"][-1]["formula"].split("=")[0].strip()
                template.write(
                    f'<pl-number-input answers-name="{answers_name}" comparison="decdig" digits="{question["formula_decimal_places"]}"></pl-number-input>\n'
                )

            elif question["question_type"] == "short_answer_question":
                answer = question["answers"][0]
                template.write(
                    f'<pl-string-input answers-name="input" correct-answer="{answer["text"]}"></pl-string-input>\n'
                )

            elif question["question_type"] == "fill_in_multiple_blanks_question":
                options = {}
                for answer in question["answers"]:
                    if answer["blank_id"] not in options:
                        options[answer["blank_id"]] = []
                    options[answer["blank_id"]].append(answer)
                for answer_id, answers in options.items():
                    question_text.replace(
                        f"[{answer_id}]",
                        f'<pl-string-input answers-name="{answer_id}" correct-answer="{answers[0]["text"]}" remove-spaces="true" ignore-case="true" display="inline"></pl-string-input>',
                    )
                template.write(question_text + "\n")

            elif question["question_type"] == "matching_question":
                template.write('<pl-matching answers-name="match">\n')
                for answer in question["answers"]:
                    template.write(f'  <pl-statement match="m{answer["match_id"]}">{answer["text"]}</pl-statement>\n')
                template.writelines(
                    f'  <pl-option name="m{match["match_id"]}">{match["text"]}</pl-option>\n'
                    for match in question["matches"]
                )
                template.write("</pl-matching>\n")

            elif question["question_type"] == "multiple_dropdowns_question":
                blanks = {}
                for answer in question["answers"]:
                    if answer["blank_id"] not in blanks:
                        blanks[answer["blank_id"]] = []
                    blanks[answer["blank_id"]].append(answer)
                for blank, answers in blanks.items():
                    dropdown = (
                        f'<pl-multiple-choice display="dropdown" hide-letter-keys="true" answers-name="{blank}">\n'
                    )
                    for answer in answers:
                        dropdown += "  <pl-answer"
                        if answer["weight"] > 0:
                            dropdown += ' correct="true"'
                        dropdown += f">{answer['text']}</pl-answer>\n"
                    dropdown += "</pl-multiple-choice>"
                    question_text = question_text.replace(f"[{blank}]", dropdown)
                template.write(question_text + "\n")

            else:
                input("Unsupported question type: " + question["question_type"])
                template.write(json.dumps(question, indent=2))

            if question["correct_comments"] or question["neutral_comments"]:
                template.write("<pl-answer-panel>\n<p>\n")
                if question.get("correct_comments_html", False):
                    template.write(question["correct_comments_html"] + "\n")
                elif question["correct_comments"]:
                    template.write(question["correct_comments"] + "\n")
                if question.get("neutral_comments_html", False):
                    template.write(question["neutral_comments_html"] + "\n")
                elif question["neutral_comments"]:
                    template.write(question["neutral_comments"] + "\n")
                template.write("</p>\n</pl-answer-panel>\n")

        if question["question_type"] == "calculated_question":
            with open(os.path.join(question_dir, "server.py"), "w") as script:
                script.write("import random\n\n")
                script.write("def generate(data):\n")
                for variable in question["variables"]:
                    if not variable.get("scale", False):
                        script.write(
                            f"    {variable['name']} = random.randint({int(variable['min'])}, {int(variable['max'])})\n"
                        )
                    else:
                        multip = 10 ** variable["scale"]
                        script.write(
                            f"    {variable['name']} = random.randint({int(variable['min'] * multip)}, {int(variable['max'] * multip)}) / {multip}\n"
                        )
                script.writelines(f"    {formula['formula']}\n" for formula in question["formulas"])
                for variable in question["variables"]:
                    script.write(f'    data["params"]["{variable["name"]}"] = {variable["name"]}\n')
                answer = question["formulas"][-1]["formula"].split("=")[0].strip()
                script.write(f'    data["correct_answers"]["{answer}"] = {answer}\n')

    with open(os.path.join(quiz_name, "infoAssessment.json"), "w") as assessment:
        pl_quiz = {
            "uuid": str(uuid.uuid4()),
            "type": assessment_type or ("Exam" if quiz["time_limit"] else "Homework"),
            "title": quiz["title"],
            "text": quiz["description"],
            "set": assessment_set,
            "number": assessment_number,
            "allowAccess": [pl_quiz_allow_access_rule],
            "zones": [{"questions": pl_quiz_questions}],
            "comment": f"Imported from Canvas, quiz {quiz['id']}",
        }
        json.dump(pl_quiz, assessment, indent=2)

    print(f"\nDONE. The assessment was created in: {quiz_name}")
