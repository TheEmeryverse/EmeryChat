import * as React from 'react'
import socket from '../socket'

const hideSendButton = true

const colors = {
    owner: '#daa520',
    admin: '#ca20d9',
    member: '#208fd9',
    bot: '#D3D3FF'
}

const sendChat = async (content) => {

    if (!content.text) {
        // later, we can check for attachments as well
        return false
    }

    try {
        const headers = new Headers();
        headers.append("Content-Type", "application/json");
        const response = await fetch('http://localhost:3002/chat/message', {
            method: 'POST',
            body: JSON.stringify({
                content: content.text,
                attactments: content.attactments,
                chat_id: content.id
            }),
            credentials: 'include',
            headers,
        })
        const res = await response
        if (res.status === 401) {
            return false
        } else {
            try {
                const body = await res.json()
                socket.emit('send-message', content.id)
                document.getElementById('Chat_Input').value = ''
                inputText('')
                return body
            } catch (error) {
                return false
            }
        }
    } catch (error) {
        console.log('failed to login: ', error)
    }
}

const fetchorCreateChat = async (updateCurrentChat) => {
    try {
        const response = await fetch(`http://localhost:3002/chat/forUser`, { credentials: 'include' })
        const data = await response.json()
        if (data.success) {
            updateCurrentChat(data.chat_data)
        }
    } catch {
        console.log('failed to connect to server')
    }
}

const fetchNewMessages = async (id, updateCurrentChat) => {
    try {
        console.log(id)
        const response = await fetch(`http://localhost:3002/chat/${id}`, { credentials: 'include' })
        const data = await response.json()
        if (data.success) {
            updateCurrentChat(data.chat_data)
        }
    } catch {
        console.log('failed to connect to server')
    }
}

const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

export const TextChat = (props) => {
    const {userData, socket} = props
    const [currentChat, updateCurrentChat] = React.useState(false)
    const [inputtedText, inputText] = React.useState('')

    const seenDates = {}
    const seenTimes = {}

    const formatedTime = (timestamp) => {
        const date = new Date(timestamp)

        let [month, day, year, hour, minutes] = [
            date.getMonth(),
            date.getDate(),
            date.getFullYear(),
            date.getHours(),
            date.getMinutes()
        ]

        let display_date = display_date = `${months[month]} ${day}, ${year}`
        let display_time = ''

        if (minutes < 10) {
            minutes = `0${minutes}`
        }

        if (hour > 12) {
            display_time = `${hour - 12}:${minutes} PM`
        } else {
            if (hour === 0) {
                display_time = `12:${minutes} AM`
            } else {
                display_time = `${hour}:${minutes} AM`
            }
        }

        if (seenDates[`${display_date}`]) {
            display_date = ''
        } else {
            seenDates[`${display_date}`] = true
        }

        if (seenTimes[`${display_time}`]) {
            display_time = ''
        } else {
            seenTimes[`${display_time}`] = true
        }

        return {
            date: `${display_date}`,
            time: `${display_time}`,
        }
    }

    const MessageContent = (props) => {
        const {message, memberDetails, i, total} = props
        const {date, time} = formatedTime(message.timestamp)

        const margin = i === total - 1 ? '50px' : '0px'

        return <>
            {date.length ?
                <div className='message_date' key={date}>{date}</div>
            : <></>}
            <div key={message.timestamp} className='message_content' style={{marginBottom: `${margin}`}}>
                <div className='message_timestamp'>{time}</div>
                <div className='message_text'>
                    <b style={{'color': colors[memberDetails[message.user]?.role ?? 'member']}}>
                        {memberDetails[message.user].name}
                    </b>
                    {message.content}
                </div>
            </div>
        </>
    }

    React.useEffect(() => {
        if (!currentChat) {
            // fetchNewMessages(userData._id, updateMessages)
            fetchorCreateChat(updateCurrentChat)
        } else if (currentChat._id) {
            console.log('joined')
            socket.emit('join-channel', currentChat._id)
        }
    }, [currentChat])

    socket.once("receive-message", (chatID) => {
        // currentChat._id check ? idk yet.
        fetchNewMessages(chatID, updateCurrentChat)
    })

    const handleKeyDown = (event) => {
        if (event.key === 'Enter' && event.target.id === 'Chat_Input' && event.target.value) {
            sendChat({
                text: inputtedText,
                id: currentChat._id,
                attactments: []
            }, inputText)
        }
      }

    return <div className="content_block chat_block">

        <div className='Chat_Messages_Container'>
            {currentChat?.messages ?
                currentChat.messages.map((message, i) =>
                    <MessageContent key={message.timestamp} message={message} memberDetails={currentChat.memberDetails} i={i} total={currentChat.messages.length}/>
                ) : <></>
            }
        </div>
        
        <div className='chat_textbox_container'>
            <input
                type="text"
                id="Chat_Input"
                name="Chat_Input"
                className='chat_textbox textBox'
                placeholder='Send Message'
                onChange={(e) => {inputText(e.target.value)}}
                onKeyDown={handleKeyDown}
            />

            {!hideSendButton ? <input
                type="button"
                value="↪"
                className='buttonPrimary chat_send_button'
                onClick={() => {
                    sendChat({
                        text: inputtedText,
                        id: currentChat._id,
                        attactments: []
                    }, inputText)
                }}
            /> : <></>}
        </div>
    </div>
}