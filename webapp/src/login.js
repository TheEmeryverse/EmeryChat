import * as React from 'react'
import {isValidUserName} from '../common/helpers'

export const Login = () => {

    const [creds, updateCreds] = React.useState({
        name: '',
        password: ''
    })

    const [errors, updateErrors] = React.useState({
        name: '',
        password: '',
        general: ''
    })

    const inputText = (field, value) => {
        updateCreds({
            ...creds,
            [field]: value
        })
    }

    const login = async (name, password) => {

        if (!name || !password) {
            let nameErr = ''
            let passwordErr = ''
            if (!name) {
                nameErr = 'Please enter your username'
            } else if (!isValidUserName(name)) {
                nameErr = 'Please enter a valid username'
            }
            if (!password) {
                passwordErr = 'Please enter your password'
            }
            updateErrors({
                ...updateErrors,
                name: nameErr,
                password: passwordErr
            })
            return
        } else if (name && !isValidUserName(name)) {
            updateErrors({
                ...updateErrors,
                name: 'Please enter a valid username',
            })
            return
        }

        try {
            const headers = new Headers();
            headers.append("Content-Type", "application/json");
            const response = await fetch('http://localhost:3000/user/login', {
                method: 'POST',
                body: JSON.stringify({
                    name,
                    password
                }),
                credentials: 'include',
                headers,
            })
            const res = await response
            if (res.status === 401) {
                updateErrors({
                    ...updateErrors,
                    general: 'Invalid credentials'
                })
            } else {
                try {
                    const body = await res.json()
                    window.location.href = body.redirect
                    updateErrors({
                        name: '',
                        password: '',
                        general: ''
                    })

                } catch (error) {
                    updateErrors({
                        ...updateErrors,
                        general: 'Something went wrong, try again later'
                    })
                }
            }
        } catch (error) {
            console.log('failed to login: ', error)
        }
    }

    return <div className='loginContainer'>
        <div className='loginBox'>
            <h1>Login</h1>
            <form
                action="#"
                className='loginForm'
                onSubmit={(event) => {
                    event.preventDefault()
                    login(creds.name, creds.password)
                }}>
                <label htmlFor="name">Username</label><br />
                <input
                    type="text"
                    id="name"
                    name="name"
                    className={`textBox ${errors.name ? 'error' : ''}`}
                    placeholder='username'
                    onChange={(e) => {inputText('name', e.target.value)}}
                />
                {
                    errors.name ? <div className='errorText'>
                    {errors.name}
                    </div> : void 0
                }
                <br />
                <label htmlFor="password">Password</label><br />
                <input
                    type="password"
                    id="password"
                    name="password"
                    className={`textBox ${errors.password ? 'error' : ''}`}
                    placeholder='password'
                    onChange={(e) => {inputText('password', e.target.value)}}
                />
                {
                    (errors.password || errors.general) ? <div className='errorText'>
                    {errors.password ? errors.password : errors.general}
                    </div> : void 0
                }
                <br />
                <div className="buttonRow">
                    <input
                        type="submit"
                        value="Login"
                        className='buttonPrimary loginButton'
                    />
                    <a href="/signup">
                        <input
                            value="Creat an Account"
                            className='buttonSecondary signupButton'
                            readOnly={true}
                        />
                    </a>
                </div>
            </form> 
        </div>
    </div>
}
