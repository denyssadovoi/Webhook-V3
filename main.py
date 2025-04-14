import os
import time
from functools import wraps
from telebot import types, TeleBot
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv
import threading
from datetime import datetime, timedelta

# Load environment variables
load_dotenv()

# === Configuration ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")     # Google Sheet with Projects & Tasks tabs
CREDENTIALS_FILE = os.environ.get("CREDENTIALS_FILE", "credentials.json")
API_RATE_LIMIT = 60

# === Authorized Telegram Usernames ===
# Add the Telegram usernames (without '@') who are allowed to use the bot
AUTHORIZED_USERNAMES = {"Denys_Sadovoi", "jmcn_ie", "username3"}  # REPLACE WITH ACTUAL USERNAMES

# === Available Assignees (for tasks and project assignee) ===
available_assignees = ["Jonathan", "Stefan", "Denys", "Pierre", "Jimmy"]

# === Initialize Google Services and Bot ===
scopes = [
    "https://www.googleapis.com/auth/spreadsheets"
]
creds = service_account.Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
sheets_service = build('sheets', 'v4', credentials=creds)
bot = TeleBot(BOT_TOKEN)

# === Global State Data ===
user_rates = {}
user_states = {}
user_auth = {}  # Store user credentials and tokens
notification_queue = []
notification_thread = None
active_chat_ids = set() # Keep track of active authorized chat IDs

# === Utility Decorators ===
def rate_limit(func):
    @wraps(func)
    def wrapper(message):
        user_id = message.from_user.id
        now = time.time()
        if user_id not in user_rates:
            user_rates[user_id] = {'tokens': API_RATE_LIMIT, 'last_check': now}
        elapsed = now - user_rates[user_id]['last_check']
        user_rates[user_id]['tokens'] = min(API_RATE_LIMIT,
                                            user_rates[user_id]['tokens'] + elapsed * (API_RATE_LIMIT / 60))
        user_rates[user_id]['last_check'] = now
        if user_rates[user_id]['tokens'] >= 1:
            user_rates[user_id]['tokens'] -= 1
            return func(message)
        else:
            bot.send_message(message.chat.id, "⏳ Please wait a moment before making another request.")
    return wrapper

def handle_errors(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except HttpError as e:
            msg = f"🚨 API Error: {e.error_details[0]['message']}" if e.error_details else "🚨 API Error occurred."
        except Exception as e:
            msg = f"⚠️ Unexpected error: {str(e)}"
        if args and isinstance(args[0], types.CallbackQuery):
            bot.answer_callback_query(args[0].id, msg)
        elif args and hasattr(args[0], 'chat'):
            bot.send_message(args[0].chat.id, msg)
        else:
            print(msg)
    return wrapper

def require_auth(func):
    @wraps(func)
    def wrapper(message_or_call):
        user = None
        chat_id = None
        # Check if it's a Message or CallbackQuery
        if isinstance(message_or_call, types.Message):
            user = message_or_call.from_user
            chat_id = message_or_call.chat.id
        elif isinstance(message_or_call, types.CallbackQuery):
            user = message_or_call.from_user
            chat_id = message_or_call.message.chat.id
        
        if user and user.username and user.username in AUTHORIZED_USERNAMES:
            # User is authorized, proceed with the function
            return func(message_or_call)
        else:
            # User is not authorized
            username_str = f" (@{user.username})" if user and user.username else ""
            unauthorized_msg = f"Sorry, user {user.first_name}{username_str} (ID: {user.id}) is not authorized to use this bot."
            if chat_id:
                try:
                    bot.send_message(chat_id, unauthorized_msg)
                except Exception as e:
                    print(f"Error sending unauthorized message: {e}")
            else:
                print(unauthorized_msg) # Log if chat_id not found
            
            # For CallbackQuery, answer it to remove the 'loading' state
            if isinstance(message_or_call, types.CallbackQuery):
                try:
                    bot.answer_callback_query(message_or_call.id, "Unauthorized Access")
                except Exception as e:
                    print(f"Error answering callback query: {e}")
            return None # Stop execution
    return wrapper

# === Menus ===
def get_initial_menu():
    menu = types.ReplyKeyboardMarkup(resize_keyboard=True)
    menu.row("Project Tracking")
    return menu

def get_project_tracking_menu():
    menu = types.ReplyKeyboardMarkup(resize_keyboard=True)
    menu.row("Project Status")
    return menu

# === Google Drive Helpers (Document Hub) ===
def get_folder_contents(folder_id, page_token=None):
    return drive_service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, mimeType, modifiedTime), nextPageToken",
        pageSize=PAGE_SIZE,
        pageToken=page_token,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()

def get_folder_path(folder_id):
    path = []
    current_id = folder_id
    while current_id != ROOT_FOLDER_ID:
        try:
            folder = drive_service.files().get(fileId=current_id, fields="name,parents").execute()
            path.append(folder['name'])
            if 'parents' in folder and folder['parents']:
                current_id = folder['parents'][0]
            else:
                break
        except:
            break
    return " ➔ ".join(reversed(path)) if path else "Root"

def show_folder(chat_id, page_token=None):
    state = user_states.setdefault(chat_id, {'current_folder': ROOT_FOLDER_ID, 'folder_history': []})
    bot.send_chat_action(chat_id, "typing")
    try:
        result = get_folder_contents(state['current_folder'], page_token)
        items = result.get('files', [])
        next_token = result.get('nextPageToken')
        markup = types.InlineKeyboardMarkup()
        for item in items:
            icon = "📁" if item['mimeType'] == "application/vnd.google-apps.folder" else "📄"
            markup.add(types.InlineKeyboardButton(f"{icon} {item['name']}", callback_data=f"item_{item['id']}"))
        nav_buttons = []
        if state.get('page_token'):
            nav_buttons.append(types.InlineKeyboardButton("◀️ Prev", callback_data=f"page_{state['page_token']}"))
        if next_token:
            nav_buttons.append(types.InlineKeyboardButton("▶️ Next", callback_data=f"page_{next_token}"))
        if nav_buttons:
            markup.row(*nav_buttons)
        path = get_folder_path(state['current_folder'])
        bot.send_message(chat_id, f"📂 *{path}*", parse_mode="Markdown", reply_markup=markup)
        state['page_token'] = page_token
    except Exception as e:
        bot.send_message(chat_id, "❌ Error loading folder. Please try again.", reply_markup=get_document_menu())

# === Project Tracking Functions ===

def build_projects_keyboard(projects):
    keyboard = types.InlineKeyboardMarkup()
    for row in projects:
        btn = types.InlineKeyboardButton(text=row[1], callback_data=f"projdetail_{row[0]}")
        keyboard.add(btn)
    return keyboard

# 1. List Projects – Grouped by Priority (with colored circles).
@bot.message_handler(func=lambda message: message.text and message.text.strip() == "Project Status")
@require_auth
@handle_errors
@rate_limit
def project_status_handler(message):
    print("Project Status Handler triggered:", message.text)
    # Make sure the correct section is set
    if message.chat.id not in user_states:
        user_states[message.chat.id] = {}
    user_states[message.chat.id]['section'] = 'project'
    # Call list_projects directly to show the projects list
    list_projects(message.chat.id)

def list_projects(chat_id):
    try:
        print(f"list_projects function called for chat_id: {chat_id}")
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Projects!A2:F1000"
        ).execute()
        rows = result.get("values", [])
        print("Projects rows retrieved:", rows)
        if not rows:
            bot.send_message(chat_id, "No projects found.", reply_markup=get_project_tracking_menu())
            return

        # Prepare all projects in a single list with priority indicators
        projects_list = []
        for row in rows:
            if len(row) < 2 or not row[0] or not row[1]:
                continue
            priority = row[3].strip().lower() if len(row) >= 4 else ""
            if priority == "high":
                priority_icon = "🔴"
            elif priority == "medium":
                priority_icon = "🟡"
            elif priority == "low":
                priority_icon = "🟢"
            else:
                priority_icon = "⚪"
                
            projects_list.append((row, priority_icon))
        
        # Send a single message with all projects
        if projects_list:
            keyboard = types.InlineKeyboardMarkup()
            for row, icon in projects_list:
                btn = types.InlineKeyboardButton(text=f"{icon} {row[1]}", callback_data=f"projdetail_{row[0]}")
                keyboard.add(btn)
            bot.send_message(chat_id, "*All Projects:*", parse_mode="Markdown", reply_markup=keyboard)
            print(f"Sent project list to chat_id: {chat_id} with {len(projects_list)} projects")
        else:
            bot.send_message(chat_id, "No projects found.", reply_markup=get_project_tracking_menu())
            print(f"No projects found for chat_id: {chat_id}")
    except Exception as e:
        print(f"Error in list_projects: {str(e)}")
        bot.send_message(chat_id, f"Error listing projects: {str(e)}", reply_markup=get_project_tracking_menu())

# 2. Show Detailed Project Information & List Associated Tasks
@bot.callback_query_handler(func=lambda call: call.data.startswith("projdetail_"))
@require_auth
@handle_errors
def handle_project_detail(call):
    project_id = call.data.split("_", 1)[1]
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Projects!A2:F1000"
        ).execute()
        rows = result.get("values", [])
        project = next((row for row in rows if len(row) >= 2 and row[0] == project_id), None)
        if not project:
            bot.answer_callback_query(call.id, "Project not found.")
            return

        project_name = project[1] if len(project) > 1 else "Unnamed"
        assignee = project[2] if len(project) > 2 and project[2].strip() else "Not assigned"
        priority = project[3] if len(project) > 3 and project[3].strip() else "Not set"
        status = project[4] if len(project) > 4 and project[4].strip() else "Unknown"
        notes = project[5] if len(project) > 5 and project[5].strip() else "No notes"

        detail_msg = f"*Project:* {project_name}\n"
        detail_msg += f"*Assignee:* {assignee}\n"
        detail_msg += f"*Priority:* {priority}\n"
        detail_msg += f"*Status:* {status}\n"
        detail_msg += f"*Notes:* {notes}\n\n"
        detail_msg += "*Tasks:*\n"

        tasks_result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Tasks!A2:E1000"
        ).execute()
        task_rows = tasks_result.get("values", [])
        tasks_for_project = []
        for task in task_rows:
            if len(task) >= 2 and task[0] == project_id:
                tdesc = task[1] if len(task) > 1 else "No description"
                tstatus = task[2] if len(task) > 2 else "Not set"
                tnotes = f" (Notes: {task[4]})" if len(task) >= 5 and task[4].strip() else ""
                tasks_for_project.append(f"• {tdesc} [{tstatus}]{tnotes}")
        if tasks_for_project:
            detail_msg += "\n".join(tasks_for_project)
        else:
            detail_msg += "No tasks found."

        # Build inline keyboard with additional project edit options.
        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(
            types.InlineKeyboardButton("Add Task", callback_data=f"projadd_{project_id}"),
            types.InlineKeyboardButton("Edit Tasks", callback_data=f"projedit_{project_id}")
        )
        keyboard.row(
            types.InlineKeyboardButton("Add Notes", callback_data=f"proj_editnotes_{project_id}"),
            types.InlineKeyboardButton("Change Priority", callback_data=f"proj_editpriority_{project_id}")
        )
        keyboard.row(
            types.InlineKeyboardButton("Change Status", callback_data=f"proj_editstatus_{project_id}"),
            types.InlineKeyboardButton("Change Assignee", callback_data=f"proj_editassignee_{project_id}")
        )
        keyboard.row(
            types.InlineKeyboardButton("Back to Projects", callback_data="projback")
        )

        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                              text=detail_msg, parse_mode="Markdown", reply_markup=keyboard)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error retrieving project: {str(e)}")

# === Assignee Multi-Selection ===
def build_assignee_keyboard(assignees_selected):
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    for assignee in available_assignees:
        selection_mark = "✅" if assignee in assignees_selected else "❌"
        keyboard.add(types.InlineKeyboardButton(f"{selection_mark} {assignee}", callback_data=f"toggle_assignee_{assignee}"))
    keyboard.row(types.InlineKeyboardButton("✓ Confirm Selection", callback_data="assignee_confirm"))
    return keyboard

# === Project Tracking Functions Continued ===

# 3. Return to the Project List.
@bot.callback_query_handler(func=lambda call: call.data == "projback")
@require_auth
@handle_errors
def handle_proj_back(call):
    list_projects(call.message.chat.id)
    bot.answer_callback_query(call.id)

# === Multi-Step New Task Addition Flow ===
@bot.callback_query_handler(func=lambda call: call.data.startswith("projadd_"))
@require_auth
@handle_errors
def initiate_add_task(call):
    project_id = call.data.split("_", 1)[1]
    state = user_states.setdefault(call.message.chat.id, {})
    state['action'] = 'add_task'
    state['add_task_project_id'] = project_id
    state['add_task_step'] = 'description'
    bot.answer_callback_query(call.id, "Let's add a new task.")
    bot.send_message(call.message.chat.id, "Enter new task Description:")

@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get('action') == 'add_task' and user_states[m.chat.id].get('add_task_step') == 'description')
@require_auth
@handle_errors
@rate_limit
def add_task_description_handler(message):
    state = user_states[message.chat.id]
    state['new_task_desc'] = message.text.strip()
    state['add_task_step'] = 'status'
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("Not Done", callback_data="task_status_Not Done"),
        types.InlineKeyboardButton("In Progress", callback_data="task_status_In Progress"),
        types.InlineKeyboardButton("Done", callback_data="task_status_Done")
    )
    bot.send_message(message.chat.id, "Select task status:", reply_markup=keyboard)

@bot.callback_query_handler(func=lambda call: call.data.startswith("task_status_") and user_states.get(call.message.chat.id, {}).get('action') == 'add_task' and user_states[call.message.chat.id].get('add_task_step') == 'status')
@require_auth
@handle_errors
def add_task_status_handler(call):
    state = user_states[call.message.chat.id]
    status_val = call.data.split("task_status_")[1]
    state['new_task_status'] = status_val
    state['add_task_step'] = 'assignee'
    state['new_task_assignees'] = []
    keyboard = build_assignee_keyboard(state['new_task_assignees'])
    bot.edit_message_text("Select assignee(s) for the task:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_assignee_") and user_states.get(call.message.chat.id, {}).get('action') == 'add_task' and user_states[call.message.chat.id].get('add_task_step') == 'assignee')
@require_auth
@handle_errors
def toggle_assignee_handler(call):
    state = user_states[call.message.chat.id]
    assignee = call.data.split("toggle_assignee_")[1]
    selected = state.get('new_task_assignees', [])
    if assignee in selected:
        selected.remove(assignee)
    else:
        selected.append(assignee)
    state['new_task_assignees'] = selected
    keyboard = build_assignee_keyboard(selected)
    bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "assignee_confirm" and user_states.get(call.message.chat.id, {}).get('action') == 'add_task' and user_states[call.message.chat.id].get('add_task_step') == 'assignee')
@require_auth
@handle_errors
def confirm_assignee_handler(call):
    state = user_states[call.message.chat.id]
    state['new_task_assignees_final'] = state.get('new_task_assignees', [])
    state['add_task_step'] = 'notes'
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("No Notes", callback_data="notes_none"))
    bot.edit_message_text("Enter additional notes for the task (or click 'No Notes'):", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "notes_none" and user_states.get(call.message.chat.id, {}).get('action') == 'add_task' and user_states[call.message.chat.id].get('add_task_step') == 'notes')
@require_auth
@handle_errors
def no_notes_handler(call):
    state = user_states[call.message.chat.id]
    state['new_task_notes'] = ""
    finalize_new_task(call.message.chat.id, call.from_user.username)
    bot.answer_callback_query(call.id, "Task added with no notes.")

@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get('action') == 'add_task' and user_states[m.chat.id].get('add_task_step') == 'notes')
@require_auth
@handle_errors
@rate_limit
def add_task_notes_handler(message):
    state = user_states[message.chat.id]
    state['new_task_notes'] = message.text.strip()
    finalize_new_task(message.chat.id, message.from_user.username)

def finalize_new_task(chat_id, username):
    state = user_states.get(chat_id, {})
    project_id = state.get('add_task_project_id')
    desc = state.get('new_task_desc', '(No Description)')
    status_val = state.get('new_task_status', '(No Status)')
    assignees = state.get('new_task_assignees_final', [])
    assignee_str = ", ".join(assignees) if assignees else ""
    notes = state.get('new_task_notes', ' ')
    new_task = [project_id, desc, status_val, assignee_str, notes]
    try:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Tasks!A:E",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [new_task]}
        ).execute()
        bot.send_message(chat_id, "Task added successfully.", reply_markup=get_project_tracking_menu())
        
        # Add notification
        project_name = get_project_name_by_id(project_id)
        add_notification(f"🔔 @{username} added task '{desc}' to project '{project_name}'")

    except Exception as e:
        bot.send_message(chat_id, f"Error adding task: {str(e)}", reply_markup=get_project_tracking_menu())
    finally:
        # Clean up state
        for k in ['action','add_task_project_id','add_task_step','new_task_desc','new_task_status','new_task_assignees','new_task_assignees_final','new_task_notes']:
            state.pop(k, None)

# === Editing Existing Task Flow ===

@bot.callback_query_handler(func=lambda call: call.data.startswith("projedit_"))
@require_auth
@handle_errors
def handle_project_edit_tasks(call):
    project_id = call.data.split("_", 1)[1]
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Tasks!A2:E1000"
        ).execute()
        rows = result.get("values", [])
        tasks = []
        row_num = 2
        for row in rows:
            if row and row[0] == project_id:
                tasks.append((row_num, row))
            row_num += 1
        if not tasks:
            bot.answer_callback_query(call.id, "No tasks to edit for this project.")
            return
        keyboard = types.InlineKeyboardMarkup()
        for rn, task in tasks:
            desc = task[1] if len(task) >= 2 else "No description"
            keyboard.add(types.InlineKeyboardButton(text=f"Edit: {desc}", callback_data=f"edittask_{project_id}_{rn}"))
        keyboard.add(types.InlineKeyboardButton("Back to Project", callback_data=f"projdetail_{project_id}"))
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                              text="Select a task to edit:", reply_markup=keyboard)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error listing tasks for editing: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("edittask_"))
@require_auth
@handle_errors
def handle_edit_task_callback(call):
    parts = call.data.split("_")
    if len(parts) < 3:
        bot.answer_callback_query(call.id, "Invalid edit task callback.")
        return
    project_id = parts[1]
    task_row = parts[2]
    state = user_states.setdefault(call.message.chat.id, {})
    state['action'] = 'edit_task'
    state['edit_task_project_id'] = project_id
    state['edit_task_row'] = task_row
    state['edit_task_step'] = 'description'
    bot.answer_callback_query(call.id, "Editing task.")
    bot.send_message(call.message.chat.id, "Enter new task Description:")

@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get('action') == 'edit_task' and user_states[m.chat.id].get('edit_task_step') == 'description')
@require_auth
@handle_errors
@rate_limit
def edit_task_description_handler(message):
    state = user_states[message.chat.id]
    state['edit_task_desc'] = message.text.strip()
    state['edit_task_step'] = 'status'
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("Not Done", callback_data="edit_task_status_Not Done"),
        types.InlineKeyboardButton("In Progress", callback_data="edit_task_status_In Progress"),
        types.InlineKeyboardButton("Done", callback_data="edit_task_status_Done")
    )
    bot.send_message(message.chat.id, "Select new task status:", reply_markup=keyboard)

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_task_status_") and user_states.get(call.message.chat.id, {}).get('action') == 'edit_task' and user_states[call.message.chat.id].get('edit_task_step') == 'status')
@require_auth
@handle_errors
def edit_task_status_handler(call):
    state = user_states[call.message.chat.id]
    status_val = call.data.split("edit_task_status_")[1]
    state['edit_task_status'] = status_val
    state['edit_task_step'] = 'assignee'
    state['edit_task_assignees'] = []
    keyboard = build_assignee_keyboard(state['edit_task_assignees'])
    bot.edit_message_text("Select new assignee(s) for the task:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_assignee_") and user_states.get(call.message.chat.id, {}).get('action') == 'edit_task' and user_states[call.message.chat.id].get('edit_task_step') == 'assignee')
@require_auth
@handle_errors
def toggle_edit_assignee_handler(call):
    state = user_states[call.message.chat.id]
    assignee = call.data.split("toggle_assignee_")[1]
    selected = state.get('edit_task_assignees', [])
    if assignee in selected:
        selected.remove(assignee)
    else:
        selected.append(assignee)
    state['edit_task_assignees'] = selected
    keyboard = build_assignee_keyboard(selected)
    bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "assignee_confirm" and user_states.get(call.message.chat.id, {}).get('action') == 'edit_task' and user_states[call.message.chat.id].get('edit_task_step') == 'assignee')
@require_auth
@handle_errors
def edit_assignee_confirm_handler(call):
    state = user_states[call.message.chat.id]
    state['edit_task_assignees_final'] = state.get('edit_task_assignees', [])
    state['edit_task_step'] = 'notes'
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("No Notes", callback_data="edit_notes_none"))
    bot.edit_message_text("Enter new additional notes (or click 'No Notes'):", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "edit_notes_none" and user_states.get(call.message.chat.id, {}).get('action') == 'edit_task' and user_states[call.message.chat.id].get('edit_task_step') == 'notes')
@require_auth
@handle_errors
def edit_no_notes_handler(call):
    state = user_states[call.message.chat.id]
    state['edit_task_notes'] = ""
    finalize_edit_task(call.message.chat.id, call.from_user.username)
    bot.answer_callback_query(call.id, "Task updated with no notes.")

@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get('action') == 'edit_task' and user_states[m.chat.id].get('edit_task_step') == 'notes')
@require_auth
@handle_errors
@rate_limit
def edit_task_notes_handler(message):
    state = user_states[message.chat.id]
    state['edit_task_notes'] = message.text.strip()
    finalize_edit_task(message.chat.id, message.from_user.username)

def finalize_edit_task(chat_id, username):
    state = user_states.get(chat_id, {})
    project_id = state.get('edit_task_project_id')
    task_row = state.get('edit_task_row')
    new_desc = state.get('edit_task_desc', '(No Description)')
    new_status = state.get('edit_task_status', '(No Status)')
    assignees = state.get('edit_task_assignees_final', [])
    assignee_str = ", ".join(assignees) if assignees else ""
    new_notes = state.get('edit_task_notes', ' ')
    # Note: Project ID (Column A) is not updated here
    new_row_data = [new_desc, new_status, assignee_str, new_notes]
    try:
        update_range = f"Tasks!B{task_row}:E{task_row}" # Update columns B to E
        body = {"values": [new_row_data]}
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=update_range,
            valueInputOption="USER_ENTERED", # Changed to USER_ENTERED
            body=body
        ).execute()
        bot.send_message(chat_id, "Task updated successfully.", reply_markup=get_project_tracking_menu())
        
        # Add notification
        project_name = get_project_name_by_id(project_id)
        add_notification(f"🔔 @{username} updated task '{new_desc}' in project '{project_name}'")

    except Exception as e:
        bot.send_message(chat_id, f"Error updating task: {str(e)}", reply_markup=get_project_tracking_menu())
    finally:
        # Clean up state
        for k in ['action','edit_task_project_id','edit_task_row','edit_task_desc','edit_task_status','edit_task_assignees','edit_task_assignees_final','edit_task_notes','edit_task_step']:
            state.pop(k, None)

# === New Project Editing Flows ===

# A. Edit/Add Project Notes
@bot.callback_query_handler(func=lambda call: call.data.startswith("proj_editnotes_"))
@require_auth
@handle_errors
def handle_project_edit_notes(call):
    parts = call.data.split("_", 2)
    if len(parts) < 3:
        bot.answer_callback_query(call.id, "Invalid callback.")
        return
    project_id = parts[2]
    state = user_states.setdefault(call.message.chat.id, {})
    state["action"] = "edit_project_notes"
    state["edit_project_id"] = project_id
    bot.answer_callback_query(call.id, "Enter new project notes:")
    bot.send_message(call.message.chat.id, "Please enter new project notes:")

@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("action") == "edit_project_notes")
@require_auth
@handle_errors
@rate_limit
def handle_edit_project_notes(m):
    state = user_states[m.chat.id]
    new_notes = m.text.strip()
    project_id = state.get("edit_project_id")
    if not project_id:
        bot.send_message(m.chat.id, "Project not found.", reply_markup=get_project_tracking_menu())
        return
    update_project_field(m.chat.id, project_id, "F", new_notes, "Project notes updated.", m.from_user.username)
    state.pop("action", None)
    state.pop("edit_project_id", None)

# B. Change Project Priority
@bot.callback_query_handler(func=lambda call: call.data.startswith("proj_editpriority_"))
@require_auth
@handle_errors
def handle_project_edit_priority(call):
    parts = call.data.split("_", 2)
    if len(parts) < 3:
        bot.answer_callback_query(call.id, "Invalid callback.")
        return
    project_id = parts[2]
    state = user_states.setdefault(call.message.chat.id, {})
    state["action"] = "edit_project_priority"
    state["edit_project_id"] = project_id
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("High 🔴", callback_data="priority_High"),
        types.InlineKeyboardButton("Medium 🟡", callback_data="priority_Medium")
    )
    keyboard.add(
        types.InlineKeyboardButton("Low 🟢", callback_data="priority_Low"),
        types.InlineKeyboardButton("Unset ⚪", callback_data="priority_Unset")
    )
    bot.edit_message_text("Select new project priority:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("priority_") and user_states.get(call.message.chat.id, {}).get("action") == "edit_project_priority")
@require_auth
@handle_errors
def priority_selection_handler(call):
    new_priority = call.data.split("priority_")[1]
    state = user_states[call.message.chat.id]
    project_id = state.get("edit_project_id")
    if not project_id:
        bot.answer_callback_query(call.id, "Project not found.")
        return
    update_project_field(call.message.chat.id, project_id, "D", new_priority, "Project priority updated.", call.from_user.username)
    state.pop("action", None)
    state.pop("edit_project_id", None)
    bot.answer_callback_query(call.id)

# C. Change Project Status
@bot.callback_query_handler(func=lambda call: call.data.startswith("proj_editstatus_"))
@require_auth
@handle_errors
def handle_project_edit_status(call):
    parts = call.data.split("_", 2)
    if len(parts) < 3:
        bot.answer_callback_query(call.id, "Invalid callback.")
        return
    project_id = parts[2]
    state = user_states.setdefault(call.message.chat.id, {})
    state["action"] = "edit_project_status"
    state["edit_project_id"] = project_id
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("Not Started", callback_data="status_Not Started"),
        types.InlineKeyboardButton("In Progress", callback_data="status_In Progress")
    )
    keyboard.add(
        types.InlineKeyboardButton("Completed", callback_data="status_Completed"),
        types.InlineKeyboardButton("On Hold", callback_data="status_On Hold")
    )
    bot.edit_message_text("Select new project status:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("status_") and user_states.get(call.message.chat.id, {}).get("action") == "edit_project_status")
@require_auth
@handle_errors
def status_selection_handler(call):
    new_status = call.data.split("status_")[1]
    state = user_states[call.message.chat.id]
    project_id = state.get("edit_project_id")
    if not project_id:
        bot.answer_callback_query(call.id, "Project not found.")
        return
    update_project_field(call.message.chat.id, project_id, "E", new_status, "Project status updated.", call.from_user.username)
    state.pop("action", None)
    state.pop("edit_project_id", None)
    bot.answer_callback_query(call.id)

# D. Change Project Assignee
@bot.callback_query_handler(func=lambda call: call.data.startswith("proj_editassignee_"))
@require_auth
@handle_errors
def handle_project_edit_assignee(call):
    parts = call.data.split("_", 2)
    if len(parts) < 3:
        bot.answer_callback_query(call.id, "Invalid callback.")
        return
    project_id = parts[2]
    state = user_states.setdefault(call.message.chat.id, {})
    state["action"] = "edit_project_assignee"
    state["edit_project_id"] = project_id
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    for name in available_assignees:
        keyboard.add(types.InlineKeyboardButton(name, callback_data=f"select_assignee_{name}"))
    bot.edit_message_text("Select new project assignee:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_assignee_") and user_states.get(call.message.chat.id, {}).get("action") == "edit_project_assignee")
@require_auth
@handle_errors
def select_assignee_handler(call):
    new_assignee = call.data.split("select_assignee_")[1]
    state = user_states[call.message.chat.id]
    project_id = state.get("edit_project_id")
    if not project_id:
        bot.answer_callback_query(call.id, "Project not found.")
        return
    update_project_field(call.message.chat.id, project_id, "C", new_assignee, "Project assignee updated.", call.from_user.username)
    state.pop("action", None)
    state.pop("edit_project_id", None)
    bot.answer_callback_query(call.id)

def update_project_field(chat_id, project_id, col_letter, new_value, success_msg, username):
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Projects!A2:A1000" # Only need column A to find the row
        ).execute()
        rows = result.get("values", [])
        row_number = None
        for idx, row in enumerate(rows, start=2):
            if row and row[0] == project_id:
                row_number = idx
                break
        
        if not row_number:
            bot.send_message(chat_id, "Project ID not found.", reply_markup=get_project_tracking_menu())
            return
            
        update_range = f"Projects!{col_letter}{row_number}"
        body = {"values": [[new_value]]}
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=update_range,
            valueInputOption="USER_ENTERED", # Changed to USER_ENTERED
            body=body
        ).execute()
        bot.send_message(chat_id, success_msg, reply_markup=get_project_tracking_menu())

        # Add notification
        project_name = get_project_name_by_id(project_id) # Get name *after* potential update if col is B
        field_name = {
            "C": "assignee", 
            "D": "priority", 
            "E": "status", 
            "F": "notes",
            "B": "name" # Added project name
        }.get(col_letter.upper(), f"column {col_letter}") # Use upper case for safety
        
        add_notification(f"🔔 @{username} changed {field_name} of project '{project_name}' to '{new_value}'")

    except Exception as e:
        bot.send_message(chat_id, f"Error updating project: {str(e)}", reply_markup=get_project_tracking_menu())

# === Section Selection Handlers ===
@bot.message_handler(func=lambda message: message.text == "Project Tracking")
@require_auth
@handle_errors
@rate_limit
def handle_project_tracking(message):
    if message.chat.id not in user_states:
        user_states[message.chat.id] = {}
    user_states[message.chat.id]['section'] = 'project'
    bot.send_message(message.chat.id, "Welcome to Project Tracking!", reply_markup=get_project_tracking_menu())

@bot.message_handler(func=lambda message: message.text == "Back to Main")
@require_auth
@handle_errors
@rate_limit
def back_to_main(message):
    if message.chat.id in user_states:
        user_states[message.chat.id]['section'] = None
    bot.send_message(message.chat.id, "Returning to main menu.", reply_markup=get_initial_menu())

# === /start Command ===
@bot.message_handler(commands=["start"])
# No auth required for /start initially, but we add user to state
@handle_errors
@rate_limit
def handle_start(message):
    # Check auth *after* potentially adding user state
    user = message.from_user
    chat_id = message.chat.id
    if not user.username or user.username not in AUTHORIZED_USERNAMES:
        username_str = f" (@{user.username})" if user.username else ""
        bot.send_message(chat_id, f"Sorry, user {user.first_name}{username_str} (ID: {user.id}) is not authorized.")
        active_chat_ids.discard(chat_id) # Remove from active list if unauthorized
        return
        
    # Add authorized user's chat_id to the active set for notifications
    active_chat_ids.add(chat_id)
    print(f"User @{user.username} started. Active chats: {active_chat_ids}") # Debugging
        
    user_states[chat_id] = {
        "section": "project",  # Set section to project immediately
        "action": None
    }
    welcome_text = "Welcome to Project Tracking Bot! This bot helps you manage your projects and tasks in Google Sheets."
    bot.send_message(chat_id, welcome_text, reply_markup=get_project_tracking_menu())
    
    # Automatically show the list of projects after starting
    list_projects(chat_id)

# === Notification Service ===
def start_notification_service():
    """Start a background thread for notification delivery."""
    def notification_worker():
        while True:
            if notification_queue:
                notification = notification_queue.pop(0)
                # Create a copy of the set to avoid issues if it changes during iteration
                current_active_chats = active_chat_ids.copy()
                print(f"Sending notification to {len(current_active_chats)} chats: {notification}") # Debugging
                for chat_id in current_active_chats:
                    try:
                        bot.send_message(chat_id, notification)
                    except Exception as e:
                        print(f"Error sending notification to chat {chat_id}: {str(e)}")
                        # Optional: Remove chat_id if sending failed persistently
                        # if 'bot was blocked' in str(e) or 'user deactivated' in str(e):
                        #    active_chat_ids.discard(chat_id)
            time.sleep(2)  # Check every 2 seconds
    
    global notification_thread
    if notification_thread is None or not notification_thread.is_alive():
        notification_thread = threading.Thread(target=notification_worker, daemon=True)
        notification_thread.start()
        print("Notification service started.")

def add_notification(message):
    """Add a notification to the queue."""
    print(f"Adding notification: {message}") # Debugging
    notification_queue.append(message)

# Helper function to get project name by ID (Consider caching this)
def get_project_name_by_id(project_id):
    try:
        # Optimize: Only fetch columns A and B
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Projects!A2:B1000" 
        ).execute()
        rows = result.get("values", [])
        for row in rows:
            if len(row) >= 1 and row[0] == project_id:
                return row[1] if len(row) >= 2 else f"Project ID {project_id}" # Return Name or ID
        return f"Project ID {project_id}" # Project ID not found
    except Exception as e:
        print(f"Error fetching project name for {project_id}: {e}")
        return f"Project ID {project_id}" # Return ID on error

# === Start Bot ===
if __name__ == "__main__":
    print("Bot starting...")
    start_notification_service() # Start the notification thread
    print("Starting polling...")
    bot.infinity_polling()
    print("Bot stopped.") # This line might not be reached in normal operation
