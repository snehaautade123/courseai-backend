from dotenv import load_dotenv
load_dotenv()
import os
import re
import bcrypt
from pymongo import MongoClient
from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from datetime import datetime
import jwt

from functools import wraps

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization")

        if not token:
            return jsonify({"message": "Token missing"}), 401

        try:
            token = token.split(" ")[1]  # remove "Bearer"
            data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            request.user_email = data["email"]
        except Exception as e:
           print("JWT Error:", str(e))
           return jsonify({"message": "Invalid token"}), 401

        return f(*args, **kwargs)

    return decorated

SECRET_KEY = os.getenv("SECRET_KEY", "dev_secret")


app = Flask(__name__)
mongo_uri = os.getenv("MONGO_URI")

if not mongo_uri:
    raise Exception("MONGO_URI not set")


client = MongoClient(mongo_uri)

db = client["courseai"]
users_collection = db["users"]
bookmarks_collection = db["bookmarks"]
users_collection.create_index([("email", 1)], unique=True)
bookmarks_collection.create_index([("user", 1)])
CORS(app)

print("Starting CourseAI Backend...")

# Load dataset
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(os.path.join(BASE_DIR, "data/coursera_courses.csv"))

# Rename columns
df.columns = df.columns.str.lower().str.replace(" ", "_")

# Convert rating safely
df["course_rating"] = pd.to_numeric(df["course_rating"], errors="coerce")

# Combine features
df["combined_features"] = df["course_description"].fillna('') + " " + df["skills"].fillna('')

# TF-IDF
vectorizer = TfidfVectorizer(stop_words='english')
tfidf_matrix = vectorizer.fit_transform(df["combined_features"])


# ---------------- ROUTES ---------------- #

@app.route("/")
def home():
    return "CourseAI Backend is running!"


@app.route("/recommend", methods=["POST"])
@token_required
def recommend():

    data = request.get_json(silent=True) or {}

    # ✅ TAKE DATA FROM FRONTEND (NOT DB)
    skill = str(data.get("skill", "")).strip().lower()
    domain = str(data.get("domain", "")).replace("-", " ").strip().lower()
    objective = str(data.get("objective", "")).strip().lower()
    difficulty = data.get("difficulty", "").lower()

    # ✅ DEBUG (MANDATORY)
    print("===== BACKEND RECEIVED =====")
    print("Skill:", skill)
    print("Domain:", domain)
    print("Objective:", objective)
    print("Difficulty:", difficulty)

    filtered = df.copy()

    # ✅ SKILL FILTER (STRICT MATCH)
    if skill:
        filtered = filtered[
            filtered["skills"]
            .fillna("")
            .str.lower()
            .str.contains(skill, case=False, na=False)
        ]

    # ✅ DIFFICULTY FILTER
    if difficulty:
        if difficulty == "beginner":
            filtered = filtered[
                filtered["difficulty_level"].str.lower().str.contains("beginner", na=False)
            ]
        elif difficulty == "intermediate":
            filtered = filtered[
                filtered["difficulty_level"].str.lower().str.contains("intermediate", na=False)
            ]
        elif difficulty == "advanced":
            filtered = filtered[
                filtered["difficulty_level"].str.lower().str.contains("advanced", na=False)
            ]

    # ✅ IF NO DATA AFTER FILTER
    if filtered.empty:
      return jsonify({
        "courses": [],
        "roadmap": []
      })

    # ✅ CREATE USER PROFILE TEXT (BOOST SKILL IMPORTANCE)
    user_text = f"{skill} {domain} {objective} {difficulty}"

    # ================= ADD RECENT BEHAVIOR =================

    user_email = request.user_email

    recent_courses = list(bookmarks_collection.find(
        {"user": user_email, "type": "recent"}
    ).sort("timestamp", -1).limit(5))

    recent_text = " ".join([
      str(course.get("course_name", "")).lower()
      for course in recent_courses
    ])

    # 🔥 BOOST USER PROFILE
    user_text = user_text + " " + recent_text

    # ✅ TF-IDF SIMILARITY
    user_vector = vectorizer.transform([user_text])

    filtered_indices = filtered.index
    filtered_matrix = tfidf_matrix[filtered_indices]

    similarity_scores = cosine_similarity(user_vector, filtered_matrix)

    # ✅ TOP 5 RESULTS
    top_indices = similarity_scores.argsort()[0][-5:][::-1]
    result = filtered.iloc[top_indices]

    # ✅ SORT BY RATING
    result = result.sort_values(by="course_rating", ascending=False)

    # ✅ CLEAN DATA FOR JSON
    result = result.replace({pd.NA: "", float("nan"): ""})
    result = result.fillna("")

    # ✅ ADD REASON
    result["reason"] = "Recommended based on your selected skill and profile"

# 🎯 ROADMAP LOGIC (NOW INSIDE FUNCTION)

    roadmap = []

    if "sql" in skill or "database" in skill:
      roadmap = [
        "Learn SQL Basics",
        "Practice Queries",
        "Advanced SQL Concepts",
        "Build Database Projects"
      ]
    elif "web" in domain:
      roadmap = [
        "Learn HTML & CSS",
        "JavaScript Fundamentals",
        "Frontend Framework (React)",
        "Build Full Projects"
      ]
    elif "data" in domain:
      roadmap = [
        "Python Basics",
        "Data Analysis (Pandas)",
        "Machine Learning",
        "Real-world Projects"
      ]
    else:
      roadmap = [
        "Start with Basics",
        "Intermediate Learning",
        "Advanced Topics",
        "Projects"
      ]

# ✅ FINAL RETURN
    return jsonify({
      "courses": result.to_dict(orient="records"),
      "roadmap": roadmap
    })


@app.route("/save-user", methods=["POST"])
@token_required
def save_user():
    data = request.get_json()
    user_email = request.user_email

    users_collection.update_one(
        {"email": user_email},
        {"$set": data},
        upsert=True
    )

    return jsonify({"message": "User saved successfully"})

@app.route("/signup", methods=["POST"])
def signup():
    data = request.get_json()

    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"message": "Missing email or password"}), 400

    # check if user already exists
    if users_collection.find_one({"email": email}):
        return jsonify({"message": "User already exists"}), 400

    # hash password
    hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

    # store user
    users_collection.insert_one({
        "email": email,
        "name": data.get("fullName"),
        "password": hashed_password
    })

    return jsonify({"message": "Signup successful"})
@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()

    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"message": "Missing email or password"}), 400

    user = users_collection.find_one({"email": email})

    if not user:
        return jsonify({"message": "User not found"}), 404

    # check password
    if bcrypt.checkpw(password.encode('utf-8'), user["password"]):
        from datetime import datetime, timedelta
        token = jwt.encode({
          "email": user["email"],
          "exp": datetime.utcnow() + timedelta(hours=24)
        }, SECRET_KEY, algorithm="HS256")

        return jsonify({
           "message": "Login successful",
           "user": {
               "email": user["email"],
               "name": user.get("name", "")
            },
            "token": token
        })
    else:
        return jsonify({"message": "Invalid password"}), 401

@app.route("/bookmark", methods=["POST"])
@token_required
def bookmark():
    data = request.get_json()

    user_email = request.user_email

    existing = bookmarks_collection.find_one({
       "course_name": data.get("course_name"),
       "user": user_email
    })

    if existing:
       return jsonify({"message": "Already bookmarked"})

    data["user"] = user_email
    bookmarks_collection.insert_one(data)

    return jsonify({"message": "Bookmarked successfully"})

@app.route("/get-bookmarks", methods=["GET"])
@token_required
def get_bookmarks():
    user = request.user_email

    bookmarks = list(bookmarks_collection.find({"user": user}, {"_id": 0}))

    return jsonify(bookmarks)

# ================= RECENT COURSES FEATURE ================= #

@app.route("/recent", methods=["POST"])
@token_required
def save_recent():
    data = request.get_json()
    user_email = request.user_email

    bookmarks_collection.update_one(
    {
        "user": user_email,
        "course_name": data.get("course_name"),
        "type": "recent"
    },
    {
        "$set": {
            "timestamp": datetime.utcnow()
        }
    },
    upsert=True
)

    return jsonify({"message": "Saved"})


@app.route("/get-recent", methods=["GET"])
@token_required
def get_recent():
    user = request.user_email

    recents = list(bookmarks_collection.find(
        {"user": user, "type": "recent"},
        {"_id": 0}
    ).sort("timestamp", -1).limit(5))

    return jsonify(recents)

@app.route("/analytics", methods=["GET"])
@token_required
def analytics():

    total_bookmarks = bookmarks_collection.count_documents({})

    # User stats
    user_stats = list(bookmarks_collection.aggregate([
        {"$group": {"_id": "$user", "count": {"$sum": 1}}}
    ]))

    # Top courses
    top_courses = list(bookmarks_collection.aggregate([
        {"$group": {"_id": "$course_name", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 5}
    ]))

    # Growth (demo)
    user_growth = [
        {"week": "Week 1", "count": 2},
        {"week": "Week 2", "count": 4},
        {"week": "Week 3", "count": 6},
        {"week": "Week 4", "count": total_bookmarks}
    ]

    # Bookmark categories
    category_stats = list(bookmarks_collection.aggregate([
        {
            "$group": {
                "_id": {
                    "$cond": [
                        {"$regexMatch": {"input": "$course_name", "regex": "ml|machine learning", "options": "i"}},
                        "ML",
                        {
                            "$cond": [
                                {"$regexMatch": {"input": "$course_name", "regex": "web|html|css|javascript", "options": "i"}},
                                "Web",
                                {
                                    "$cond": [
                                        {"$regexMatch": {"input": "$course_name", "regex": "data|analysis", "options": "i"}},
                                        "Data Science",
                                        {
                                            "$cond": [
                                                {"$regexMatch": {"input": "$course_name", "regex": "cloud|aws", "options": "i"}},
                                                "Cloud",
                                                "Other"
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                },
                "count": {"$sum": 1}
            }
        }
    ]))

    # ================= DATASET ANALYTICS ================= #
    total_courses = len(df)

    dataset_category_count = {
        "ML": 0,
        "Web": 0,
        "Data Science": 0,
        "Cloud": 0,
        "Other": 0
    }

    for _, row in df.iterrows():
        name = str(row.get("course_name") or row.get("course_title") or "").lower()
        desc = str(row.get("course_description") or "").lower()
        skills = str(row.get("skills") or "").lower()

        text = name + " " + desc + " " + skills

        if "machine learning" in text or "deep learning" in text:
            dataset_category_count["ML"] += 1

        elif "web" in text or "html" in text or "javascript" in text:
            dataset_category_count["Web"] += 1

        elif "data" in text or "analysis" in text or "analytics" in text:
            dataset_category_count["Data Science"] += 1

        elif "cloud" in text or "aws" in text:
            dataset_category_count["Cloud"] += 1

        else:
            dataset_category_count["Other"] += 1

    return jsonify({
    "total_bookmarks": total_bookmarks,
    "user_stats": user_stats,
    "top_courses": top_courses,
    "user_growth": user_growth,
    "category_stats": category_stats,
    "total_courses": total_courses,
    "dataset_categories": dataset_category_count
    })
# ---------------- RUN ---------------- #
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)



